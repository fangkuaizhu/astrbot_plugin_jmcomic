"""
JMComic 客户端封装
"""

import re
import os
import logging
from typing import List

logger = logging.getLogger(__name__)


class JMApiClient:
    """JMComic 客户端"""
    
    def __init__(self, client_impl: str = 'api'):
        if not is_available():
            raise ImportError("jmcomic not installed")
        
        import jmcomic
        # 使用自定义 option 减少重试和超时
        self._option = jmcomic.JmOption.default()
        self._option.client.retry_times = 1
        # 通过 postman meta_data 传递 curl_cffi 超时参数
        meta = self._option.client.postman.get('meta_data', {})
        meta.setdefault('timeout', 10)
        self._option.client.postman.meta_data = meta
        self._client = self._option.build_jm_client()
        logger.info(f"JMComic client initialized (impl={client_impl}, retry={self._option.client.retry_times})")
    
    def search(self, keyword: str, page: int = 1) -> dict:
        """
        搜索本子
        
        Args:
            keyword: 搜索关键词
            page: 页码
            
        Returns:
            dict: {'results': [{'id': str, 'title': str}], 'total_pages': int, 'current_page': int}
        """
        result = self._client.search_site(keyword, page)
        
        albums = []
        for album_id, title in result.iter_id_title():
            albums.append({'id': album_id, 'title': title})
        
        return {
            'results': albums,
            'total_pages': result.page_count,
            'current_page': page,
        }
    
    def download_album(self, album_id: str, save_dir: str, cancel_event=None) -> List[str]:
        """
        下载本子所有章节到指定目录
        
        遍历 album.episode_list 中的所有话，全局顺序编号，
        确保多章节本子的图片按阅读顺序排列。
        
        Returns:
            List[str]: 下载的图片路径列表（按阅读顺序）
        """
        album_id = self._extract_id(album_id)
        os.makedirs(save_dir, exist_ok=True)
        
        # 获取本子详情
        album = self._client.get_album_detail(album_id)
        episodes = album.episode_list
        if not episodes:
            raise ValueError("No episodes found")
        
        import time as _time
        logger.debug(f"[JM] Album has {len(episodes)} episode(s), starting...")
        _t0 = _time.time()
        
        image_paths = []
        global_idx = 0
        failed = 0
        
        logger.info(f"Album: {album.title} | {len(episodes)} episode(s), starting download...")
        
        for idx, episode in enumerate(episodes, 1):
            if cancel_event and cancel_event.is_set():
                logger.info(f"Download cancelled at episode {idx} (album_id={album_id})")
                break
            
            photo_id = episode[0]
            _te = _time.time()
            
            photo = self._client.get_photo_detail(photo_id)
            ep_img_count = 0
            
            for img_detail in photo:
                if cancel_event and cancel_event.is_set():
                    logger.info(f"Download cancelled mid-episode {idx}")
                    break
                try:
                    global_idx += 1
                    ext = os.path.splitext(img_detail.img_url)[1] if hasattr(img_detail, 'img_url') else '.webp'
                    img_path = os.path.join(save_dir, f'{global_idx:05d}{ext}')
                    
                    logger.debug(f"[JM] Fetching img {global_idx}: {os.path.basename(img_detail.img_url)}")
                    self._client.download_by_image_detail(img_detail, img_path)
                    logger.debug(f"[JM] Saved img {global_idx}: {img_path}")
                    image_paths.append(img_path)
                    ep_img_count += 1
                except Exception as e:
                    failed += 1
                    logger.warning(f"Failed to download image (episode {idx}, img {global_idx}): {e}")
            
            elapsed = _time.time() - _te
            logger.info(f"  Episode {idx}/{len(episodes)}: {ep_img_count} images in {elapsed:.1f}s (photo_id={photo_id})")
        
        total_time = _time.time() - _t0
        logger.info(f"Download complete: {len(image_paths)} images from {len(episodes)} episode(s) in {total_time:.1f}s")
        if failed:
            logger.warning(f"{failed} image(s) failed, PDF will have gaps")
        
        return image_paths
    
    def _extract_id(self, album_id: str) -> int:
        """提取本子ID"""
        if str(album_id).isdigit():
            return int(album_id)
        
        patterns = [
            r'(?:JM|jm)(\d+)',
            r'/album/(\d+)',
            r'/photo/(\d+)',
            r'(\d{4,})',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, str(album_id))
            if match:
                return int(match.group(1))
        
        raise ValueError(f"无法识别车号: {album_id}")


import sys

_client = None


def get_jm_client(client_impl: str = 'api') -> JMApiClient:
    global _client
    if _client is None:
        _client = JMApiClient(client_impl)
    return _client


def is_available() -> bool:
    """检查 jmcomic 库是否可用（每次调用实时检测，支持热安装后识别）"""
    try:
        import jmcomic
        return True
    except ImportError:
        return False
