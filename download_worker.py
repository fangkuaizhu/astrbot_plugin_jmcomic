"""
独立进程下载 Worker
避免 curl_cffi 的 GIL 阻塞事件循环导致 WebSocket 断连
支持逐话生成 PDF + 分页下载
"""

import os
import sys
import json
import logging
from typing import Optional, List

sys.path.insert(0, '/AstrBot/data/plugins/astrbot_plugin_jmcomic')

logger = logging.getLogger(__name__)

CHAPTERS_PER_PAGE = 10


def _write_progress(path: str, phase: str, current: int, total: int, extra: dict = None):
    try:
        pct = int(current / max(total, 1) * 100) if total > 0 else 0
        data = {"phase": phase, "current": current, "total": total, "pct": pct}
        if extra:
            data.update(extra)
        with open(path, 'w') as f:
            json.dump(data, f)
    except Exception:
        pass


def _count_pdf_pages(raw: bytes) -> int:
    return raw.count(b'/Type /Page') - raw.count(b'/Type /Pages')


def _convert_webp_to_jpg(paths: List[str]) -> List[str]:
    """将列表中的 webp 转为 jpg，返回新路径列表"""
    from PIL import Image
    out = []
    for p in paths:
        if not p.lower().endswith('.webp'):
            out.append(p)
            continue
        try:
            jpg = p.rsplit('.', 1)[0] + '.jpg'
            with Image.open(p) as img:
                if img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')
                img.save(jpg, 'JPEG', quality=95)
            os.remove(p)
            out.append(jpg)
        except Exception:
            out.append(p)
    return out


def run_download(
    album_id: str,
    temp_root: str,
    page_num: int = 1,
    client_impl: str = 'api',
    max_pages: int = 300,
    cancel_signal_path: Optional[str] = None,
    progress_path: Optional[str] = None,
) -> dict:
    """
    下载指定页的章节，每话独立出 PDF。
    返回 {ok, album_id, page_num, ch_start, ch_end, total_ch, pdfs: [{path, pages, size}], error}
    """
    try:
        from pdf_maker import PDFMaker
        import jmcomic

        opt = jmcomic.JmOption.default()
        opt.client.retry_times = 1
        meta = opt.client.postman.get('meta_data', {})
        meta.setdefault('timeout', 10)
        opt.client.postman.meta_data = meta
        client = opt.build_jm_client()

        tmpdir = os.path.join(temp_root, str(album_id))
        os.makedirs(tmpdir, exist_ok=True)

        if cancel_signal_path and os.path.exists(cancel_signal_path):
            os.remove(cancel_signal_path)
            return {'ok': False, 'error': 'cancelled_before_start'}

        # 获取本子信息
        try:
            ext_id = _extract_album_id(album_id)
            album = client.get_album_detail(ext_id)
            episodes = album.episode_list
            if not episodes:
                return {'ok': False, 'error': 'no episodes'}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

        total_ch = len(episodes)

        # 计算此页的章节范围
        if page_num is None or page_num == 'all' or (isinstance(page_num, int) and page_num <= 0):
            ch_start, ch_end = 1, total_ch
        else:
            pn = int(page_num)
            ch_start = (pn - 1) * CHAPTERS_PER_PAGE + 1
            ch_end = min(pn * CHAPTERS_PER_PAGE, total_ch)

        if ch_start > total_ch:
            return {
                'ok': False, 'album_id': album_id, 'page_num': page_num,
                'ch_start': ch_start, 'ch_end': ch_end, 'total_ch': total_ch,
                'error': f"page {page_num} out of range ({total_ch} chapters total)",
                'pdfs': []
            }

        _write_progress(progress_path, 'download', ch_start, total_ch,
                       {'page': f'ch{ch_start}-ch{ch_end}'})

        pdfs = []
        for ch_abs_idx in range(ch_start, ch_end + 1):
            # 取消检查
            if cancel_signal_path and os.path.exists(cancel_signal_path):
                os.remove(cancel_signal_path)
                return {'ok': False, 'error': 'cancelled', 'pdfs': pdfs}

            episode = episodes[ch_abs_idx - 1]
            chapter_pdf = os.path.join(tmpdir, f'chapter_{ch_abs_idx:03d}.pdf')

            # 跳过已缓存章节（带基本校验）
            if os.path.exists(chapter_pdf) and os.path.getsize(chapter_pdf) > 0:
                # 检查 PDF 头部防止残损
                with open(chapter_pdf, 'rb') as _fp:
                    if _fp.read(4) == b'%PDF':
                        # 不读全文，只读前 8KB 数页码
                        _fp.seek(0)
                        _head = _fp.read(8192)
                        cp = _head.count(b'/Type /Page')
                        pdfs.append({'path': chapter_pdf, 'pages': max(cp, 1), 'size': os.path.getsize(chapter_pdf)})
                        _write_progress(progress_path, 'download', ch_abs_idx, total_ch,
                                       {'page': f'ch{ch_start}-ch{ch_end}', 'current_ch': ch_abs_idx})
                        continue
                # 损坏的缓存，删掉重下
                try:
                    os.remove(chapter_pdf)
                except Exception:
                    pass

            # 下载该话（失败跳过，不丢失已完成的章节）
            try:
                photo_id = episode[0]
                photo = client.get_photo_detail(photo_id)

                # 并行下载该话所有图片（3 线程，I/O 密集无 GIL 问题）
                import concurrent.futures as _cf
                chapter_imgs_ordered = []
                with _cf.ThreadPoolExecutor(max_workers=3) as _img_pool:
                    _futs = {}
                    for page_idx, img_detail in enumerate(photo, 1):
                        ext = os.path.splitext(img_detail.img_url)[1] if hasattr(img_detail, 'img_url') else '.webp'
                        ext = ext or '.jpg'
                        img_path = os.path.join(tmpdir, f'ch{ch_abs_idx:03d}_{page_idx:04d}{ext}')
                        _futs[_img_pool.submit(client.download_by_image_detail, img_detail, img_path)] = (page_idx, img_path)
                    
                    for _fut in _cf.as_completed(_futs):
                        page_idx, img_path = _futs[_fut]
                        try:
                            _fut.result()
                            chapter_imgs_ordered.append((page_idx, img_path))
                        except Exception as e:
                            logger.warning(f"Failed img {page_idx} ch{ch_abs_idx}: {e}")
                
                if cancel_signal_path and os.path.exists(cancel_signal_path):
                    os.remove(cancel_signal_path)
                    return {'ok': False, 'error': 'cancelled', 'pdfs': pdfs}
                
                # 按页码排序后取路径
                chapter_imgs_ordered.sort(key=lambda x: x[0])
                chapter_imgs = [p for _, p in chapter_imgs_ordered]

                if not chapter_imgs:
                    logger.warning(f"No images for chapter {ch_abs_idx}, skip")
                    continue

                # WebP → JPG
                chapter_imgs = _convert_webp_to_jpg(chapter_imgs)

                if len(chapter_imgs) > max_pages:
                    chapter_imgs = chapter_imgs[:max_pages]

                # 生成该话 PDF
                PDFMaker.images_to_pdf(chapter_imgs, chapter_pdf)

                # 清理该话图片
                for img in chapter_imgs:
                    try:
                        os.remove(img)
                    except Exception:
                        pass

                if not os.path.exists(chapter_pdf) or os.path.getsize(chapter_pdf) == 0:
                    logger.warning(f"Empty PDF for chapter {ch_abs_idx}, skip")
                    continue

                with open(chapter_pdf, 'rb') as _fp:
                    _head = _fp.read(8192)
                    cp = _head.count(b'/Type /Page')
                pdfs.append({'path': chapter_pdf, 'pages': max(cp, 1), 'size': os.path.getsize(chapter_pdf)})
                _write_progress(progress_path, 'download', ch_abs_idx, total_ch,
                               {'page': f'ch{ch_start}-ch{ch_end}', 'current_ch': ch_abs_idx})

            except Exception as e:
                logger.warning(f"Chapter {ch_abs_idx} failed: {e}, skip")
                # 尝试清理残图
                for f in os.listdir(tmpdir):
                    if f.startswith(f'ch{ch_abs_idx:03d}_'):
                        try:
                            os.remove(os.path.join(tmpdir, f))
                        except Exception:
                            pass
                continue

        _write_progress(progress_path, 'pdf', len(pdfs), max(1, ch_end - ch_start + 1),
                       {'page': f'ch{ch_start}-ch{ch_end}', 'done': True})

        return {
            'ok': True,
            'album_id': album_id,
            'page_num': page_num,
            'ch_start': ch_start,
            'ch_end': ch_end,
            'total_ch': total_ch,
            'pdfs': pdfs,
            'error': None,
        }

    except Exception as e:
        return {'ok': False, 'error': str(e), 'pdfs': []}


def _extract_album_id(album_id: str) -> int:
    import re
    if str(album_id).isdigit():
        return int(album_id)
    for pattern in [r'(?:JM|jm)(\d+)', r'/album/(\d+)', r'/photo/(\d+)', r'(\d{4,})']:
        match = re.search(pattern, str(album_id))
        if match:
            return int(match.group(1))
    raise ValueError(f"无法识别车号: {album_id}")
