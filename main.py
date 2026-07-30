"""
JMComic AstrBot 插件
提供禁漫天堂本子PDF下载功能
"""

import os
import asyncio
import json
import logging
import shutil
import threading
import concurrent.futures
from datetime import datetime, time, timedelta
from typing import List, Optional
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
import astrbot.api.message_components as Comp
from astrbot.api import logger as astrbot_logger

from .jm_client import get_jm_client, is_available
from .pdf_maker import PDFMaker

module_logger = logging.getLogger(__name__)

JM_TEMP_ROOT = os.path.join('/AstrBot/data', 'jmcomic_temp')


class JMComicPlugin(Star):
    
    def __init__(self, context: Context):
        super().__init__(context)
        self.config = context.get_config() or {}
        self.client_impl = self.config.get('client_impl', 'api')
        self.max_pages = self.config.get('max_pages', 300)
        self.jm_temp_root = self.config.get('jm_temp_root', None) or JM_TEMP_ROOT
        
        self.whitelist_enabled = self.config.get('whitelist_enabled', False)
        self.group_whitelist = self.config.get('group_whitelist', [])
        self.group_blacklist = self.config.get('group_blacklist', [])
        astrbot_logger.info(f"Group access: enabled={self.whitelist_enabled}, whitelist={self.group_whitelist}, blacklist={self.group_blacklist}")
        
        self._client = None
        if not is_available():
            astrbot_logger.error("jmcomic not installed! Run: pip install jmcomic")
        os.makedirs(self.jm_temp_root, exist_ok=True)
        
        self._download_lock = asyncio.Lock()
        self._cancel_event = threading.Event()
        self._current_task_album_id = None
        self._current_progress = None  # 供 /jm进度 查询
        self._dl_pool = concurrent.futures.ProcessPoolExecutor(max_workers=1)
        self._cleanup_task = asyncio.create_task(self._scheduled_cleanup())
        # 启动时清理中断残留的零散图片文件
        self._cleanup_orphan_images()
        astrbot_logger.info("JMComic plugin initialized")
    
    def _is_group_allowed(self, event: AstrMessageEvent) -> bool:
        group_id = getattr(event.message_obj, 'group_id', None)
        if group_id is None:
            return True
        group_id = str(group_id)
        if group_id in self.group_blacklist:
            return False
        if self.whitelist_enabled:
            return group_id in self.group_whitelist
        return True
    
    def _get_client(self):
        if self._client is None:
            self._client = get_jm_client(self.client_impl)
        return self._client
    
    async def _scheduled_cleanup(self):
        while True:
            try:
                now = datetime.now()
                tomorrow = now.date() + timedelta(days=1)
                tomorrow_5am = datetime.combine(tomorrow, time(5, 0))
                wait_seconds = (tomorrow_5am - now).total_seconds()
                astrbot_logger.info(f"Next cleanup at {tomorrow_5am}, waiting {wait_seconds:.0f}s")
                await asyncio.sleep(wait_seconds)
                astrbot_logger.info("Scheduled cleanup starting...")
                async with self._download_lock:
                    self._cleanup_old_files()
                astrbot_logger.info("Scheduled cleanup completed")
            except asyncio.CancelledError:
                break
            except Exception as e:
                astrbot_logger.error(f"Scheduled cleanup error: {e}")
                await asyncio.sleep(3600)
    
    def _cleanup_old_files(self):
        try:
            if os.path.exists(self.jm_temp_root):
                for item in os.listdir(self.jm_temp_root):
                    item_path = os.path.join(self.jm_temp_root, item)
                    try:
                        if os.path.isdir(item_path):
                            shutil.rmtree(item_path, ignore_errors=True)
                        else:
                            os.remove(item_path)
                    except Exception as e:
                        module_logger.warning(f"Failed to remove {item_path}: {e}")
        except Exception as e:
            astrbot_logger.error(f"Cleanup failed: {e}")
    
    def _cleanup_orphan_images(self):
        """清理中断下载残留的零散图片，保留 chapter PDF 和标记文件"""
        if not os.path.exists(self.jm_temp_root):
            return
        img_exts = {'.webp', '.jpg', '.jpeg', '.png', '.gif'}
        for album_dir in os.listdir(self.jm_temp_root):
            dir_path = os.path.join(self.jm_temp_root, album_dir)
            if not os.path.isdir(dir_path):
                continue
            cleaned = 0
            for root, dirs, files in os.walk(dir_path):
                # 跳过顶级目录（保留 chapter PDF 所在位置）
                if root == dir_path:
                    # 只删根目录下的图片文件（非 PDF、非标记文件）
                    for f in files:
                        if os.path.splitext(f)[1].lower() in img_exts:
                            try:
                                os.remove(os.path.join(root, f))
                                cleaned += 1
                            except Exception:
                                pass
                else:
                    # 子目录里的全部清掉（旧版 images/ 目录）
                    import shutil
                    try:
                        shutil.rmtree(root, ignore_errors=True)
                        cleaned += 1
                    except Exception:
                        pass
            if cleaned:
                astrbot_logger.info(f"Cleaned {cleaned} orphan images from {album_dir}")
    
    @filter.command("jm搜索")
    async def jm_search(self, event: AstrMessageEvent, keyword: Optional[str] = None, page: int = 1):
        event.stop_event()
        if not keyword:
            yield event.plain_result("❌ 请提供搜索关键词\n示例: /jm搜索 原神")
            return
        if not is_available():
            yield event.plain_result("❌ jmcomic 库未安装")
            return
        if not self._is_group_allowed(event):
            yield event.plain_result("❌ 本群组未授权使用此插件")
            return
        try:
            import concurrent.futures
            def _search_work():
                return get_jm_client(self.client_impl).search(keyword, page)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _pool:
                try:
                    data = _pool.submit(_search_work).result(timeout=20)
                except concurrent.futures.TimeoutError:
                    yield event.plain_result(f"❌ 搜索超时: [{keyword}]，请稍后重试")
                    return
            results = data.get('results', [])
            total_pages = data.get('total_pages', 0)
            if not results:
                yield event.plain_result(f"❌ 没有找到关于 [{keyword}] 的结果")
                return
            msg_parts = [f"🔍 搜索结果: {keyword} (第{page}页)\n"]
            for i, item in enumerate(results, 1):
                msg_parts.append(f"{i}. 📖 {item['title']}\n   🆔 {item['id']}")
            if total_pages > 1:
                msg_parts.append(f"\n📄 共 {total_pages} 页")
            msg_parts.append("💡 使用 /jm <车号> 下载")
            yield event.plain_result('\n'.join(msg_parts))
        except Exception as e:
            astrbot_logger.error(f"[JM] Search failed: {e}")
            yield event.plain_result(f"❌ 搜索失败: {str(e)}")
    
    @filter.command("jm进度")
    async def jm_progress(self, event: AstrMessageEvent):
        event.stop_event()
        p = self._current_progress
        if not p:
            yield event.plain_result("📭 当前没有进行中的下载任务")
            return
        phase = p.get('phase', 'download')
        cur = p.get('current', 0)
        tot = p.get('total', 0)
        ep = p.get('episode', '')
        aid = p.get('album_id', '?')
        labels = {'download': '📥 下载中', 'convert': '🔄 转换格式', 'pdf': '📄 生成 PDF'}
        label = labels.get(phase, phase)
        ep_info = f" | 第{ep}话" if ep else ""
        if tot > 0:
            pct = p.get('pct', 0)
            yield event.plain_result(f"{label} [{aid}]: {pct}% ({cur}/{tot}){ep_info}")
        else:
            yield event.plain_result(f"{label} [{aid}]: 已下载 {cur} 张图{ep_info}")
    
    @filter.command("jmstop")
    async def jm_stop(self, event: AstrMessageEvent):
        event.stop_event()
        if not self._download_lock.locked():
            yield event.plain_result("📭 当前没有进行中的下载任务")
            return
        self._cancel_event.set()
        astrbot_logger.info(f"Cancel requested for album: {self._current_task_album_id}")
        yield event.plain_result(f"🛑 已发送打断信号")
    
    @filter.command("jm")
    async def jm_command(self, event: AstrMessageEvent, album_id: Optional[str] = None, page: Optional[str] = None):
        event.stop_event()
        if not album_id:
            yield event.plain_result("❌ 请提供车号\n示例: /jm 350234")
            return
        if not is_available():
            yield event.plain_result("❌ jmcomic 库未安装")
            return
        if not self._is_group_allowed(event):
            yield event.plain_result("❌ 本群组未授权使用此插件")
            return
        
        # 解析 page 参数
        page_num = 1  # 默认
        if page is not None:
            if page.lower() == 'all':
                page_num = 'all'
            elif page.isdigit():
                page_num = int(page)
            else:
                yield event.plain_result("❌ page 参数无效，请输入数字或 'all'\n示例: /jm 350236 /jm 350236 2 /jm 350236 all")
                return
        
        tmpdir = os.path.join(self.jm_temp_root, str(album_id))
        os.makedirs(tmpdir, exist_ok=True)
        
        # 统计缓存
        cached_chs = []
        for i in range(1, 1000):
            p = os.path.join(tmpdir, f'chapter_{i:03d}.pdf')
            if os.path.exists(p) and os.path.getsize(p) > 0:
                with open(p, 'rb') as fp:
                    if fp.read(4) == b'%PDF':
                        cached_chs.append(i)
            else:
                break
        
        umo = event.unified_msg_origin
        
        # 并发限制
        if self._download_lock.locked():
            yield event.plain_result("⏳ 有其他下载任务进行中，请稍后再试...")
            return
        
        # 定义后台下载任务
        async def _bg():
            try:
                async with self._download_lock:
                    self._cancel_event.clear()
                    self._current_task_album_id = album_id
                    
                    cancel_file = os.path.join(tmpdir, '.cancel')
                    progress_file = os.path.join(tmpdir, '.progress')
                    for f in (cancel_file, progress_file):
                        if os.path.exists(f):
                            os.remove(f)
                    
                    from .download_worker import run_download, CHAPTERS_PER_PAGE
                    
                    astrbot_logger.info(f"[JM] Start (process) album_id={album_id}, page={page_num}")
                    
                    pool = self._dl_pool
                    fut = pool.submit(
                        run_download,
                        album_id,
                        self.jm_temp_root,
                        page_num,
                        self.client_impl,
                        self.max_pages,
                        cancel_file,
                        progress_file,
                    )
                    
                    t0 = __import__('time').time()
                    while True:
                        try:
                            result = await asyncio.wait_for(asyncio.wrap_future(fut), timeout=3.0)
                            break
                        except (asyncio.TimeoutError, concurrent.futures.TimeoutError):
                            elapsed = __import__('time').time() - t0
                            
                            if self._cancel_event.is_set():
                                open(cancel_file, 'w').close()
                                fut.cancel()
                                old_pool = self._dl_pool
                                self._dl_pool = concurrent.futures.ProcessPoolExecutor(max_workers=1)
                                old_pool.shutdown(wait=False, cancel_futures=True)
                                await self._send_msg(umo, "🛑 下载已取消")
                                return
                            
                            if elapsed > 3600:
                                old_pool = self._dl_pool
                                self._dl_pool = concurrent.futures.ProcessPoolExecutor(max_workers=1)
                                old_pool.shutdown(wait=False, cancel_futures=True)
                                await self._send_msg(umo, "❌ 下载超时（60 分钟）")
                                return
                            
                            # 静默更新进度
                            if os.path.exists(progress_file):
                                try:
                                    with open(progress_file) as pf:
                                        p = json.loads(pf.read())
                                    p['album_id'] = album_id
                                    self._current_progress = p
                                except Exception:
                                    pass
                    
                    if not result.get('ok'):
                        err = result.get('error', '')
                        if 'cancel' in (err or '').lower():
                            await self._send_msg(umo, "🛑 下载已取消")
                        elif 'out of range' in (err or ''):
                            total_ch = result.get('total_ch', '?')
                            await self._send_msg(umo, f"⚠️ 该本子只有 {total_ch} 话，没有更多了")
                        else:
                            await self._send_msg(umo, f"❌ 下载失败: {err[:80] if err else '未知错误'}")
                        return
                    
                    pdfs = result.get('pdfs', [])
                    if not pdfs:
                        await self._send_msg(umo, "❌ 没有生成任何 PDF")
                        return
                    
                    ch_start = result.get('ch_start', 1)
                    ch_end = result.get('ch_end', len(pdfs))
                    total_ch = result.get('total_ch', '?')
                    
                    await self._send_msg(umo, f"📄 共生成 {len(pdfs)} 个 PDF（第{ch_start}-{ch_end}话 / 共{total_ch}话）")
                    
                    for pdf in pdfs:
                        path = pdf['path']
                        pages = pdf['pages']
                        fname = os.path.basename(path)
                        await self._send_file(umo, path, f"JM{album_id}_{fname}")
                        await asyncio.sleep(0.5)  # 避免消息风暴
                    
                    astrbot_logger.info(f"[JM] Done {album_id}: {len(pdfs)} PDFs, {sum(p['pages'] for p in pdfs)}p")
                    if isinstance(page_num, int):
                        await self._send_msg(umo, f"✅ 第{ch_start}-{ch_end}话下载完成\n💡 继续发送 /jm {album_id} {page_num + 1} 下载下一批")
                    else:
                        await self._send_msg(umo, f"✅ 全部 {ch_end} 话下载完成")
            except Exception as e:
                astrbot_logger.error(f"[JM] Background crash: {e}")
                await self._send_msg(umo, f"❌ {str(e)[:80]}")
        
        asyncio.create_task(_bg())
        page_info = f"（第 {page_num} 批）" if isinstance(page_num, int) and page_num > 1 else ""
        cache_info = f"（已缓存 {len(cached_chs)} 话）" if cached_chs else ""
        yield event.plain_result(f"📥 正在下载 [{album_id}]{page_info}{cache_info}")
    
    async def _send_msg(self, target, text: str):
        """target: AstrMessageEvent 或 unified_msg_origin 字符串"""
        try:
            from astrbot.core.message.message_event_result import MessageChain
            from astrbot.api.message_components import Plain
            umo = target.unified_msg_origin if hasattr(target, 'unified_msg_origin') else target
            await self.context.send_message(umo, MessageChain([Plain(text)]))
        except Exception as e:
            astrbot_logger.error(f"[JM] send_msg failed: {e}")
    
    async def _send_file(self, target, path: str, name: str):
        """target: AstrMessageEvent 或 unified_msg_origin 字符串"""
        try:
            from astrbot.core.message.message_event_result import MessageChain
            umo = target.unified_msg_origin if hasattr(target, 'unified_msg_origin') else target
            await self.context.send_message(umo, MessageChain([Comp.File(file=path, name=name)]))
        except Exception as e:
            astrbot_logger.error(f"[JM] send_file failed: {e}")
    
    def _verify_pdf(self, pdf_path: str, expected_pages: int = 0) -> bool:
        try:
            if not os.path.exists(pdf_path):
                return False
            size = os.path.getsize(pdf_path)
            if size == 0:
                astrbot_logger.warning(f"[JM] PDF empty: {pdf_path}")
                os.remove(pdf_path)
                return False
            with open(pdf_path, 'rb') as f:
                raw = f.read()
            actual_pages = raw.count(b'/Type /Page') - raw.count(b'/Type /Pages')
            if expected_pages > 0 and actual_pages != expected_pages:
                astrbot_logger.warning(f"[JM] PDF page mismatch: expected {expected_pages}, got {actual_pages}")
                os.remove(pdf_path)
                return False
            astrbot_logger.info(f"[JM] PDF OK: {actual_pages}p, {size//1024}KB")
            return True
        except Exception as e:
            astrbot_logger.error(f"[JM] PDF verify error: {e}")
            return False
    
    def _collect_images(self, directory: str) -> List[str]:
        exts = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
        images = []
        if not os.path.exists(directory):
            return images
        for root, _, files in os.walk(directory):
            for f in sorted(files):
                if f.lower().endswith('.pdf'):
                    continue
                if os.path.splitext(f)[1].lower() in exts:
                    images.append(os.path.join(root, f))
        return images
    
    async def terminate(self):
        if self._cleanup_task:
            self._cleanup_task.cancel()
        self._dl_pool.shutdown(wait=False)
        astrbot_logger.info("JMComic plugin terminated")
