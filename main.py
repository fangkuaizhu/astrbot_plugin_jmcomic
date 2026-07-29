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

logger = logging.getLogger(__name__)

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
        
        # 临时文件根目录（默认与 NapCat 共享路径 /AstrBot/data/jmcomic_temp）
        self.jm_temp_root = self.config.get('jm_temp_root', None) or JM_TEMP_ROOT
        
        # 白名单/黑名单配置
        self.whitelist_enabled = self.config.get('whitelist_enabled', False)
        self.group_whitelist = self.config.get('group_whitelist', [])
        self.group_blacklist = self.config.get('group_blacklist', [])
        logger.info(f"Group access: enabled={self.whitelist_enabled}, whitelist={self.group_whitelist}, blacklist={self.group_blacklist}")
        
        # 初始化组件
        self._client = None
        
        if not is_available():
            logger.error("jmcomic not installed! Run: pip install jmcomic")
        
        # 确保临时目录存在
        os.makedirs(self.jm_temp_root, exist_ok=True)
        
        # 并发控制锁
        self._download_lock = asyncio.Lock()
        
        # 打断控制（使用 threading.Event 以跨线程池边界生效）
        self._cancel_event = threading.Event()
        self._current_task_album_id = None
        
        # 启动定时清理任务
        self._cleanup_task = asyncio.create_task(self._scheduled_cleanup())
        
        logger.info("JMComic plugin initialized")
    
    def _is_group_allowed(self, event: AstrMessageEvent) -> bool:
        """检查当前群组是否允许使用插件（私聊默认放行）"""
        group_id = getattr(event.message_obj, 'group_id', None)
        if group_id is None:
            return True  # 私聊默认放行
        
        group_id = str(group_id)
        
        # 黑名单优先：在黑名单中则拒绝
        if group_id in self.group_blacklist:
            return False
        
        # 白名单模式：不在白名单中则拒绝（白名单为空时全部拒绝）
        if self.whitelist_enabled:
            return group_id in self.group_whitelist
        
        return True
    
    def _get_client(self):
        if self._client is None:
            self._client = get_jm_client(self.client_impl)
        return self._client
    
    async def _scheduled_cleanup(self):
        """每天凌晨5点清理临时文件"""
        while True:
            try:
                # 计算距离明天5点的秒数
                now = datetime.now()
                tomorrow = now.date() + timedelta(days=1)
                tomorrow_5am = datetime.combine(tomorrow, time(5, 0))
                
                wait_seconds = (tomorrow_5am - now).total_seconds()
                logger.info(f"Next cleanup at {tomorrow_5am}, waiting {wait_seconds:.0f}s")
                
                await asyncio.sleep(wait_seconds)
                
                # 执行清理（与下载互斥，确保不删正在写入的文件）
                logger.info("Scheduled cleanup starting...")
                async with self._download_lock:
                    self._cleanup_old_files()
                logger.info("Scheduled cleanup completed")
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scheduled cleanup error: {e}")
                await asyncio.sleep(3600)  # 出错后1小时重试
    
    def _cleanup_old_files(self):
        """清理临时目录中的所有文件"""
        try:
            if os.path.exists(self.jm_temp_root):
                logger.info(f"Starting cleanup of {self.jm_temp_root}")
                # 删除目录下所有内容
                for item in os.listdir(self.jm_temp_root):
                    item_path = os.path.join(self.jm_temp_root, item)
                    try:
                        if os.path.isdir(item_path):
                            shutil.rmtree(item_path, ignore_errors=True)
                            logger.info(f"Removed directory: {item_path}")
                        else:
                            os.remove(item_path)
                            logger.info(f"Removed file: {item_path}")
                    except Exception as e:
                        logger.warning(f"Failed to remove {item_path}: {e}")
                
                logger.info(f"Cleanup completed in {self.jm_temp_root}")
        except Exception as e:
            logger.error(f"Cleanup failed: {e}")
    
    @filter.command("jm搜索")
    async def jm_search(self, event: AstrMessageEvent, keyword: Optional[str] = None, page: int = 1):
        """
        搜索本子
        用法: /jm搜索 <关键词> [页码]
        示例: /jm搜索 原神
              /jm搜索 原神 2
        """
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
            # 搜索全过程（单次 yield，避免 stoprushingiamtyping 多次 yield 时卡住）
            import concurrent.futures
            def _search_work():
                from .jm_client import get_jm_client
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
            logger.error(f"[JM] Search failed: {e}")
            yield event.plain_result(f"❌ 搜索失败: {str(e)}")
    
    @filter.command("jmstop")
    async def jm_stop(self, event: AstrMessageEvent):
        """
        打断当前下载任务
        用法: /jmstop
        """
        event.stop_event()
        
        if not self._download_lock.locked():
            yield event.plain_result("📭 当前没有进行中的下载任务")
            return
        
        album_id = self._current_task_album_id
        self._cancel_event.set()
        logger.info(f"Cancel requested for album: {album_id}")
        yield event.plain_result(f"🛑 已发送打断信号，正在停止下载 [{album_id or '未知'}]...")
    
    @filter.command("jm")
    async def jm_command(self, event: AstrMessageEvent, album_id: Optional[str] = None):
        """
        下载本子PDF
        用法: /jm <车号>
        示例: /jm 350234
        """
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
        
        # 使用固定的临时目录
        tmpdir = os.path.join(self.jm_temp_root, str(album_id))
        pdf_path = os.path.join(tmpdir, f'JM{album_id}.pdf')
        
        # 检查缓存：如果PDF已存在，直接发送
        if os.path.exists(pdf_path):
            astrbot_logger.info(f"Cache hit for {album_id}, path: {pdf_path}")
            yield event.chain_result([
                Comp.File(file=pdf_path, name=f"JM{album_id}.pdf")
            ])
            return
        
        # 并发限制：同一时间只能处理一个下载
        if self._download_lock.locked():
            yield event.plain_result("⏳ 有其他下载任务进行中，请稍后再试...")
            return
        
        # 同一时间只能处理一个下载
        if self._download_lock.locked():
            yield event.plain_result("⏳ 有其他下载任务进行中，请稍后再试...")
            return
        
        # 下载全过程：客户端创建 + 下载 + PDF 都在后台线程执行，避免阻塞
        import concurrent.futures
        def _dl_work():
            from .jm_client import get_jm_client as _get_jm
            import shutil as _sh
            c = _get_jm(self.client_impl)
            c.download_album(album_id, save_dir, self._cancel_event)
            imgs = self._collect_images(save_dir)
            astrbot_logger.info(f"[JM] dl_work: collected {len(imgs)} images, cancel={self._cancel_event.is_set()}")
            if not imgs or self._cancel_event.is_set():
                return None
            if len(imgs) > self.max_pages:
                astrbot_logger.info(f"[JM] dl_work: truncated {len(imgs)} -> {self.max_pages}")
                imgs = imgs[:self.max_pages]
            PDFMaker.images_to_pdf(imgs, pdf_path)
            pdf_sz = os.path.getsize(pdf_path) if os.path.exists(pdf_path) else 0
            astrbot_logger.info(f"[JM] dl_work: PDF {pdf_sz} bytes for {len(imgs)} images")
            return imgs
        
        async with self._download_lock:
            os.makedirs(tmpdir, exist_ok=True)
            save_dir = os.path.join(tmpdir, 'images')
            self._cancel_event.clear()
            self._current_task_album_id = album_id
            astrbot_logger.info(f"[JM] Start download album_id={album_id}")
            
            _t0 = __import__('time').time()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _pool:
                _fut = _pool.submit(_dl_work)
                try:
                    images = _fut.result(timeout=600)
                except concurrent.futures.TimeoutError:
                    _pool.shutdown(wait=False)
                    yield event.plain_result("❌ 下载超时")
                    return
                _pool.shutdown(wait=False)
            
            if images is None:
                if self._cancel_event.is_set():
                    yield event.plain_result("🛑 下载已取消")
                else:
                    yield event.plain_result("❌ 下载失败")
                return
            
            if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
                yield event.plain_result("❌ 下载失败（PDF 为空）")
                return
            
            pdf_size = os.path.getsize(pdf_path)
            astrbot_logger.info(f"[JM] Done {album_id}: {len(images)}p -> {pdf_size//1024}KB PDF")
            
            try:
                with open(pdf_path, 'rb') as _f:
                    _f.read(10)
            except Exception as _e:
                astrbot_logger.error(f"[JM] PDF not readable: {_e}")
                yield event.plain_result("❌ 下载失败（文件不可读）")
                return
            
            yield event.chain_result([
                Comp.File(file=pdf_path, name=f"JM{album_id}.pdf")
            ])
    
    def _collect_images(self, directory: str) -> List[str]:
        """收集目录中的图片文件"""
        exts = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
        images = []
        
        if not os.path.exists(directory):
            return images
        
        for root, _, files in os.walk(directory):
            for f in sorted(files):
                # 排除PDF文件，只收集图片
                if f.lower().endswith('.pdf'):
                    continue
                if os.path.splitext(f)[1].lower() in exts:
                    images.append(os.path.join(root, f))
        
        return images
    
    async def terminate(self):
        """插件卸载时取消清理任务"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
        logger.info("JMComic plugin terminated")
