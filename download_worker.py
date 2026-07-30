"""
独立进程下载 Worker
避免 curl_cffi 的 GIL 阻塞事件循环导致 WebSocket 断连
"""

import os
import sys
import json
import logging
from typing import Optional

# 确保子进程能找到插件目录
sys.path.insert(0, '/AstrBot/data/plugins/astrbot_plugin_jmcomic')

logger = logging.getLogger(__name__)


def _write_progress(path: str, phase: str, current: int, total: int, extra: dict = None):
    """写进度文件供主进程读取"""
    try:
        data = {"phase": phase, "current": current, "total": total,
                "pct": int(current / max(total, 1) * 100)}
        if extra:
            data.update(extra)
        with open(path, 'w') as f:
            json.dump(data, f)
    except Exception:
        pass


def _count_pdf_pages(raw: bytes) -> int:
    return raw.count(b'/Type /Page') - raw.count(b'/Type /Pages')


def run_download(
    album_id: str,
    temp_root: str,
    client_impl: str = 'api',
    max_pages: int = 300,
    cancel_signal_path: Optional[str] = None,
    progress_path: Optional[str] = None,
) -> dict:
    """
    在独立进程中执行完整的下载→PDF 流程。
    返回 {"ok": bool, "pdf_path": str | None, "pages": int, "size_bytes": int, "error": str | None}
    """
    try:
        # 延迟导入，避免主进程的模块状态干扰
        from pdf_maker import PDFMaker
        
        # jmcomic 客户端创建
        import jmcomic
        opt = jmcomic.JmOption.default()
        opt.client.retry_times = 1
        meta = opt.client.postman.get('meta_data', {})
        meta.setdefault('timeout', 10)
        opt.client.postman.meta_data = meta
        client = opt.build_jm_client()
        
        # 目录准备
        tmpdir = os.path.join(temp_root, str(album_id))
        pdf_path = os.path.join(tmpdir, f'JM{album_id}.pdf')
        os.makedirs(tmpdir, exist_ok=True)
        save_dir = os.path.join(tmpdir, 'images')
        os.makedirs(save_dir, exist_ok=True)
        
        # 取消信号检查
        if cancel_signal_path and os.path.exists(cancel_signal_path):
            os.remove(cancel_signal_path)
            return {'ok': False, 'pdf_path': None, 'pages': 0, 'size_bytes': 0, 'error': 'cancelled_before_start'}
        
        # 下载本子
        try:
            ext_id = _extract_album_id(album_id)
            album = client.get_album_detail(ext_id)
            episodes = album.episode_list
            if not episodes:
                return {'ok': False, 'pdf_path': None, 'pages': 0, 'size_bytes': 0, 'error': 'no episodes'}
        except Exception as e:
            return {'ok': False, 'pdf_path': None, 'pages': 0, 'size_bytes': 0, 'error': str(e)}
        
        image_paths = []
        global_idx = 0
        
        # 估算总页数（用于进度）
        total_est = sum(len(ep) for ep in episodes)
        _write_progress(progress_path, 'download', 0, total_est,
                       {'episode': f'0/{len(episodes)}'})
        
        for ep_idx, episode in enumerate(episodes, 1):
            # 取消信号检查
            if cancel_signal_path and os.path.exists(cancel_signal_path):
                os.remove(cancel_signal_path)
                return {'ok': False, 'pdf_path': None, 'pages': 0, 'size_bytes': 0, 'error': 'cancelled'}
            
            photo_id = episode[0]
            photo = client.get_photo_detail(photo_id)
            
            for img_detail in photo:
                if cancel_signal_path and os.path.exists(cancel_signal_path):
                    os.remove(cancel_signal_path)
                    return {'ok': False, 'pdf_path': None, 'pages': 0, 'size_bytes': 0, 'error': 'cancelled'}
                try:
                    global_idx += 1
                    ext = os.path.splitext(img_detail.img_url)[1] if hasattr(img_detail, 'img_url') else '.webp'
                    img_path = os.path.join(save_dir, f'{global_idx:05d}{ext}')
                    client.download_by_image_detail(img_detail, img_path)
                    image_paths.append(img_path)
                    # 每张图写一次进度
                    _write_progress(progress_path, 'download', global_idx, total_est,
                                   {'episode': f'{ep_idx}/{len(episodes)}'})
                except Exception as e:
                    logger.warning(f"Failed to download image {global_idx}: {e}")
        
        if not image_paths:
            return {'ok': False, 'pdf_path': None, 'pages': 0, 'size_bytes': 0, 'error': 'no images downloaded'}
        
        # 收集图片并转换 webp → jpeg（img2pdf 不支持 webp）
        _write_progress(progress_path, 'convert', 0, len(all_imgs))
        exts = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
        all_imgs = []
        converted = 0
        for root, _, files in os.walk(save_dir):
            for f in sorted(files):
                fpath = os.path.join(root, f)
                ext = os.path.splitext(f)[1].lower()
                if ext not in exts:
                    continue
                if ext == '.webp':
                    try:
                        from PIL import Image
                        jpg_path = fpath.rsplit('.', 1)[0] + '.jpg'
                        with Image.open(fpath) as img:
                            if img.mode in ('RGBA', 'P'):
                                img = img.convert('RGB')
                            img.save(jpg_path, 'JPEG', quality=95)
                        os.remove(fpath)
                        all_imgs.append(jpg_path)
                    except Exception:
                        all_imgs.append(fpath)
                else:
                    all_imgs.append(fpath)
                converted += 1
                if converted % 50 == 0:
                    _write_progress(progress_path, 'convert', converted, len(all_imgs))
        
        if len(all_imgs) > max_pages:
            all_imgs = all_imgs[:max_pages]
        
        # 生成 PDF
        _write_progress(progress_path, 'pdf', 0, len(all_imgs))
        PDFMaker.images_to_pdf(all_imgs, pdf_path)
        
        if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
            return {'ok': False, 'pdf_path': None, 'pages': 0, 'size_bytes': 0, 'error': 'PDF empty'}
        
        size = os.path.getsize(pdf_path)
        with open(pdf_path, 'rb') as f:
            pages = _count_pdf_pages(f.read())
        
        return {'ok': True, 'pdf_path': pdf_path, 'pages': pages, 'size_bytes': size, 'error': None}
        
    except Exception as e:
        return {'ok': False, 'pdf_path': None, 'pages': 0, 'size_bytes': 0, 'error': str(e)}


def _extract_album_id(album_id: str) -> int:
    """提取本子 ID"""
    import re
    if str(album_id).isdigit():
        return int(album_id)
    for pattern in [r'(?:JM|jm)(\d+)', r'/album/(\d+)', r'/photo/(\d+)', r'(\d{4,})']:
        match = re.search(pattern, str(album_id))
        if match:
            return int(match.group(1))
    raise ValueError(f"无法识别车号: {album_id}")
