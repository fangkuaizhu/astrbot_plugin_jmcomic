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
        self.client_impl = self.config.get('client_impl', 'html')
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
    async def jm_search(self, event: AstrMessageEvent, keyword: Optional[str] = None):
        """
        搜索本子
        用法: /jm搜索 <关键词>
        示例: /jm搜索 原神
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
            yield event.plain_result(f"🔍 搜索中: {keyword}...")
            
            # 在新线程中创建客户端 + 执行搜索，整体 20 秒超时
            import concurrent.futures, functools
            def _search_work():
                from .jm_client import get_jm_client
                c = get_jm_client(self.client_impl)
                return c.search(keyword, 1)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _pool:
                _fut = _pool.submit(_search_work)
                try:
                    data = _fut.result(timeout=20)
                except concurrent.futures.TimeoutError:
                    logger.error(f"[JM] Search timeout for '{keyword}'")
                    yield event.plain_result(f"❌ 搜索超时: [{keyword}]，请稍后重试")
                    _pool.shutdown(wait=False)
                    return
            
            results = data.get('results', [])
            total_pages = data.get('total_pages', 0)
            
            if not results:
                yield event.plain_result(f"❌ 没有找到关于 [{keyword}] 的结果")
                return
            
            # 构建结果消息
            msg_parts = [f"🔍 搜索结果: {keyword}\n"]
            
            for i, item in enumerate(results, 1):
                msg_parts.append(f"{i}. 📖 {item['title']}")
                msg_parts.append(f"   🆔 {item['id']}")
            
            msg_parts.append(f"\n📄 共 {total_pages} 页")
            msg_parts.append(f"💡 使用 /jm <车号> 下载")
            
            yield event.plain_result('\n'.join(msg_parts))
            
        except Exception as e:
            logger.error(f"Search failed: {e}")
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
            logger.info(f"Cache hit for {album_id}, path: {pdf_path}")
            yield event.chain_result([
                Comp.File(file=pdf_path, name=f"JM{album_id}.pdf")
            ])
            return
        
        # 并发限制：同一时间只能处理一个下载
        if self._download_lock.locked():
            yield event.plain_result("⏳ 有其他下载任务进行中，请稍后再试...")
            return
        
        async with self._download_lock:
            os.makedirs(tmpdir, exist_ok=True)
            
            try:
                yield event.plain_result(f"📥 正在下载 [{album_id}]...")
                logger.info(f"[JM] Start download album_id={album_id}")
                
                # 重置打断信号
                self._cancel_event.clear()
                self._current_task_album_id = album_id
                
                client = self._get_client()
                loop = asyncio.get_event_loop()
                
                save_dir = os.path.join(tmpdir, 'images')
                _t0 = __import__('time').time()
                await loop.run_in_executor(
                    None,
                    client.download_album,
                    album_id,
                    save_dir,
                    self._cancel_event
                )
                dl_time = __import__('time').time() - _t0
                
                # 检查是否被打断
                if self._cancel_event.is_set():
                    shutil.rmtree(tmpdir, ignore_errors=True)
                    yield event.plain_result("🛑 下载已取消")
                    return
                
                images = self._collect_images(save_dir)
                if not images:
                    yield event.plain_result("❌ 下载失败，没有获取到图片")
                    return
                
                if len(images) > self.max_pages:
                    logger.info(f"[JM] Album {album_id}: {len(images)} images, truncated to {self.max_pages}")
                    images = images[:self.max_pages]
                else:
                    logger.info(f"[JM] Album {album_id}: {len(images)} images collected in {dl_time:.1f}s")
                
                _t1 = __import__('time').time()
                await loop.run_in_executor(
                    None,
                    PDFMaker.images_to_pdf,
                    images,
                    pdf_path,
                    f"JM{album_id}"
                )
                pdf_time = __import__('time').time() - _t1
                
                if not os.path.exists(pdf_path):
                    yield event.plain_result("❌ 下载失败")
                    return
                
                pdf_size = os.path.getsize(pdf_path)
                total_time = dl_time + pdf_time
                logger.info(f"[JM] Done {album_id}: {len(images)}p -> {pdf_size//1024}KB PDF in {total_time:.1f}s (dl={dl_time:.1f}s+pdf={pdf_time:.1f}s)")
                
                yield event.chain_result([
                    Comp.File(file=pdf_path, name=f"JM{album_id}.pdf")
                ])
                
            except Exception as e:
                if self._cancel_event.is_set():
                    shutil.rmtree(tmpdir, ignore_errors=True)
                    yield event.plain_result("🛑 下载已取消")
                else:
                    logger.error(f"[JM] Failed {album_id}: {e}")
                    yield event.plain_result(f"❌ 下载失败: {str(e)}")
            finally:
                self._current_task_album_id = None
    
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
