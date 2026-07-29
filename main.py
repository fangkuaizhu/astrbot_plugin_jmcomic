"""
JMComic AstrBot 插件
提供禁漫天堂本子PDF下载功能
"""

import os
import asyncio
import logging
import shutil
import threading
from datetime import datetime, time, timedelta
from typing import List, Optional
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
import astrbot.api.message_components as Comp
from astrbot.api import logger as astrbot_logger

from .jm_client import get_jm_client, is_available
from .pdf_maker import PDFMaker

module_logger = logging.getLogger(__name__)

# 临时文件根目录（可通过 _conf_schema.json 中的 jm_temp_root 配置）
JM_TEMP_ROOT = os.path.join('/AstrBot/data', 'jmcomic_temp')


class JMComicPlugin(Star):
    """JMComic PDF下载插件"""
    
    def __init__(self, context: Context):
        super().__init__(context)
        
        # 配置
        self.config = context.get_config() or {}
        self.client_impl = self.config.get('client_impl', 'api')
        self.max_pages = self.config.get('max_pages', 300)
        
        # 临时文件根目录
        self.jm_temp_root = self.config.get('jm_temp_root', None) or JM_TEMP_ROOT
        
        # 白名单/黑名单配置
        self.whitelist_enabled = self.config.get('whitelist_enabled', False)
        self.group_whitelist = self.config.get('group_whitelist', [])
        self.group_blacklist = self.config.get('group_blacklist', [])
        astrbot_logger.info(f"Group access: enabled={self.whitelist_enabled}, whitelist={self.group_whitelist}, blacklist={self.group_blacklist}")
        
        # 初始化组件
        self._client = None
        
        if not is_available():
            astrbot_logger.error("jmcomic not installed! Run: pip install jmcomic")
        
        os.makedirs(self.jm_temp_root, exist_ok=True)
        
        # 并发控制锁
        self._download_lock = asyncio.Lock()
        
        # 打断控制
        self._cancel_event = threading.Event()
        self._current_task_album_id = None
        
        # 定时清理
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
                c = get_jm_client(self.client_impl)
                return c.search(keyword, page)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _pool:
                _fut = _pool.submit(_search_work)
                try:
                    data = _fut.result(timeout=20)
                except concurrent.futures.TimeoutError:
                    _pool.shutdown(wait=False)
                    yield event.plain_result(f"❌ 搜索超时: [{keyword}]，请稍后重试")
                    return
            
            results = data.get('results', [])
            total_pages = data.get('total_pages', 0)
            if not results:
                yield event.plain_result(f"❌ 没有找到关于 [{keyword}] 的结果")
                return
            msg_parts = [f"🔍 搜索结果: {keyword} (第{page}页)\n"]
            for i, item in enumerate(results, 1):
                msg_parts.append(f"{i}. 📖 {item['title']}")
                msg_parts.append(f"   🆔 {item['id']}")
            if total_pages > 1:
                msg_parts.append(f"\n📄 共 {total_pages} 页")
            msg_parts.append(f"💡 使用 /jm <车号> 下载")
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
        album_id = self._current_task_album_id
        self._cancel_event.set()
        astrbot_logger.info(f"Cancel requested for album: {album_id}")
        yield event.plain_result(f"🛑 已发送打断信号，正在停止下载 [{album_id or '未知'}]...")
    
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
        
        # 缓存命中检查（含完整性校验）
        if os.path.exists(pdf_path):
            if self._verify_pdf(pdf_path):
                yield event.chain_result([Comp.File(file=pdf_path, name=f"JM{album_id}.pdf")])
                return
            astrbot_logger.info(f"[JM] Cache invalid for {album_id}, re-downloading...")
        
        # 并发限制
        if self._download_lock.locked():
            yield event.plain_result("⏳ 有其他下载任务进行中，请稍后再试...")
            return
        
        # 提前占锁，防止间隙期第二个命令也发"正在下载"
        async with self._download_lock:
            yield event.plain_result(f"📥 正在下载 [{album_id}]...")
        
        # 后台下载任务（完成后通过 context.send_message 发送文件）
        async def _background_dl():
            async with self._download_lock:
                os.makedirs(tmpdir, exist_ok=True)
                save_dir = os.path.join(tmpdir, 'images')
                self._cancel_event.clear()
                self._current_task_album_id = album_id
                astrbot_logger.info(f"[JM] Start download album_id={album_id}")
                
                import concurrent.futures
                def _dl_work():
                    c = get_jm_client(self.client_impl)
                    c.download_album(album_id, save_dir, self._cancel_event)
                    imgs = self._collect_images(save_dir)
                    astrbot_logger.info(f"[JM] dl_work: {len(imgs)} images")
                    if not imgs or self._cancel_event.is_set():
                        return None
                    if len(imgs) > self.max_pages:
                        astrbot_logger.info(f"[JM] dl_work: truncated {len(imgs)} -> {self.max_pages}")
                        imgs = imgs[:self.max_pages]
                    PDFMaker.images_to_pdf(imgs, pdf_path)
                    sz = os.path.getsize(pdf_path) if os.path.exists(pdf_path) else 0
                    astrbot_logger.info(f"[JM] dl_work: PDF {sz} bytes, {len(imgs)} images")
                    return imgs
                
                pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                try:
                    images = await asyncio.get_event_loop().run_in_executor(pool, _dl_work)
                finally:
                    pool.shutdown(wait=False)
                
                if images is None:
                    msg = "🛑 下载已取消" if self._cancel_event.is_set() else "❌ 下载失败"
                    try:
                        await self.context.send_message(event.unified_msg_origin, msg)
                    except:
                        pass
                    return
                
                if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
                    try:
                        await self.context.send_message(event.unified_msg_origin, "❌ 下载失败（PDF 为空）")
                    except:
                        pass
                    return
                
                astrbot_logger.info(f"[JM] Done {album_id}: {os.path.getsize(pdf_path)//1024}KB PDF")
                try:
                    from astrbot.api.message_components import MessageChain
                    await self.context.send_message(
                        event.unified_msg_origin,
                        MessageChain([Comp.File(file=pdf_path, name=f"JM{album_id}.pdf")])
                    )
                except Exception as e:
                    astrbot_logger.error(f"[JM] Send failed: {e}")
        
        asyncio.create_task(_background_dl())
    
    def _verify_pdf(self, pdf_path: str, expected_pages: int = 0) -> bool:
        try:
            if not os.path.exists(pdf_path):
                return False
            size = os.path.getsize(pdf_path)
            if size == 0:
                astrbot_logger.warning(f"[JM] PDF empty (0 bytes): {pdf_path}")
                os.remove(pdf_path)
                return False
            with open(pdf_path, 'rb') as f:
                raw = f.read()
            actual_pages = raw.count(b'/Type /Page') - raw.count(b'/Type /Pages')
            if expected_pages > 0 and actual_pages != expected_pages:
                astrbot_logger.warning(f"[JM] PDF page mismatch: expected {expected_pages}, got {actual_pages}")
                os.remove(pdf_path)
                return False
            astrbot_logger.info(f"[JM] PDF OK: {actual_pages} pages, {size//1024}KB")
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
        astrbot_logger.info("JMComic plugin terminated")
