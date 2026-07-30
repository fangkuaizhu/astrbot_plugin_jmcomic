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
        self._dl_pool = concurrent.futures.ProcessPoolExecutor(max_workers=1)
        self._cleanup_task = asyncio.create_task(self._scheduled_cleanup())
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
    async def jm_command(self, event: AstrMessageEvent, album_id: Optional[str] = None):
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
        
        tmpdir = os.path.join(self.jm_temp_root, str(album_id))
        pdf_path = os.path.join(tmpdir, f'JM{album_id}.pdf')
        
        # 缓存命中
        if os.path.exists(pdf_path):
            if self._verify_pdf(pdf_path):
                yield event.chain_result([Comp.File(file=pdf_path, name=f"JM{album_id}.pdf")])
                return
            astrbot_logger.info(f"[JM] Cache invalid for {album_id}, re-downloading...")
        
        # 并发限制
        if self._download_lock.locked():
            yield event.plain_result("⏳ 有其他下载任务进行中，请稍后再试...")
            return
        
        # 定义后台下载任务（必须在 yield 之前 create_task）
        async def _bg():
            try:
                async with self._download_lock:
                    os.makedirs(tmpdir, exist_ok=True)
                    self._cancel_event.clear()
                    self._current_task_album_id = album_id
                    astrbot_logger.info(f"[JM] Start (process) album_id={album_id}")
                    
                    cancel_file = os.path.join(tmpdir, '.cancel')
                    progress_file = os.path.join(tmpdir, '.progress')
                    # 清除旧进度
                    for f in (cancel_file, progress_file):
                        if os.path.exists(f):
                            os.remove(f)
                    
                    from .download_worker import run_download
                    
                    pool = self._dl_pool
                    fut = pool.submit(
                        run_download,
                        album_id,
                        self.jm_temp_root,
                        self.client_impl,
                        self.max_pages,
                        cancel_file,
                        progress_file
                    )
                    # 轮询（总超时 1800 秒 = 30 分钟兜底）
                    t0 = __import__('time').time()
                    last_progress_report = 0  # 上次汇报进度的时间戳
                    last_reported_pct = -1
                    
                    while True:
                        try:
                            result = await asyncio.wait_for(asyncio.wrap_future(fut), timeout=3.0)
                            break  # 下载完成
                        except (asyncio.TimeoutError, concurrent.futures.TimeoutError):
                            elapsed = __import__('time').time() - t0
                            
                            # 用户取消 → 真杀子进程
                            if self._cancel_event.is_set():
                                open(cancel_file, 'w').close()
                                fut.cancel()
                                # 关闭池以杀死运行中的 worker，然后重建
                                old_pool = self._dl_pool
                                self._dl_pool = concurrent.futures.ProcessPoolExecutor(max_workers=1)
                                old_pool.shutdown(wait=False, cancel_futures=True)
                                await self._send_msg(event, "🛑 下载已取消")
                                return
                            
                            # 60 分钟硬超时（防止真死锁，大专辑 CDN 慢）
                            if elapsed > 3600:
                                old_pool = self._dl_pool
                                self._dl_pool = concurrent.futures.ProcessPoolExecutor(max_workers=1)
                                old_pool.shutdown(wait=False, cancel_futures=True)
                                await self._send_msg(event, "❌ 下载超时（30 分钟）")
                                return
                            
                            # 读取进度（每 5 秒汇报一次）
                            now = __import__('time').time()
                            if now - last_progress_report >= 5 and os.path.exists(progress_file):
                                try:
                                    with open(progress_file) as pf:
                                        p = json.loads(pf.read())
                                    pct = p.get('pct', 0)
                                    phase = p.get('phase', 'download')
                                    cur = p.get('current', 0)
                                    tot = p.get('total', 0)
                                    # 仅在进度变化 ≥5% 或切换阶段时汇报
                                    if pct != last_reported_pct and (pct - last_reported_pct >= 5 or phase != getattr(self, '_last_phase', '')):
                                        last_reported_pct = pct
                                        self._last_phase = phase
                                        last_progress_report = now
                                        labels = {'download': '📥 下载中', 'convert': '🔄 转换格式', 'pdf': '📄 生成 PDF'}
                                        label = labels.get(phase, phase)
                                        await self._send_msg(event, f"{label} [{album_id}]: {pct}% ({cur}/{tot})")
                                except Exception:
                                    pass
                    
                    if not result['ok']:
                        err = result.get('error', '')
                        if 'cancel' in (err or '').lower():
                            await self._send_msg(event, "🛑 下载已取消")
                        else:
                            await self._send_msg(event, f"❌ 下载失败: {err[:80] if err else '未知错误'}")
                        return
                    
                    if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
                        await self._send_msg(event, "❌ 下载失败（PDF 为空）")
                        return
                    astrbot_logger.info(f"[JM] Done {album_id}: {result['size_bytes']//1024}KB PDF, {result['pages']}p")
                    await self._send_file(event, pdf_path, f"JM{album_id}.pdf")
            except Exception as e:
                astrbot_logger.error(f"[JM] Background crash: {e}")
                await self._send_msg(event, f"❌ {str(e)[:80]}")
        
        asyncio.create_task(_bg())
        yield event.plain_result(f"📥 正在下载 [{album_id}]...")
    
    async def _send_msg(self, event: AstrMessageEvent, text: str):
        try:
            from astrbot.core.message.message_event_result import MessageChain
            from astrbot.api.message_components import Plain
            await self.context.send_message(event.unified_msg_origin, MessageChain([Plain(text)]))
        except Exception as e:
            astrbot_logger.error(f"[JM] send_msg failed: {e}")
    
    async def _send_file(self, event: AstrMessageEvent, path: str, name: str):
        try:
            from astrbot.core.message.message_event_result import MessageChain
            await self.context.send_message(event.unified_msg_origin, MessageChain([Comp.File(file=path, name=name)]))
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
