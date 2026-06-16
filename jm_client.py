"""
JMComic 客户端封装
"""

import re
import os
import logging
from typing import List

logger = logging.getLogger(__name__)

try:
    import jmcomic
    JMCOMIC_AVAILABLE = True
except ImportError:
    JMCOMIC_AVAILABLE = False


class JMApiClient:
    """JMComic 客户端"""
    
    def __init__(self, client_impl: str = 'api'):
        if not JMCOMIC_AVAILABLE:
            raise ImportError("jmcomic not installed")
        
        self._option = jmcomic.JmOption.default()
        self._client = self._option.build_jm_client()
        logger.info(f"JMComic client initialized ({client_impl})")
    
    def search(self, keyword: str, page: int = 1, limit: int = 10) -> dict:
        """
        搜索本子
        
        Args:
            keyword: 搜索关键词
            page: 页码
            limit: 每页数量
            
        Returns:
            dict: {
                'results': [{'id': str, 'title': str, 'tags': list}],
                'total_pages': int,
                'current_page': int
            }
        """
        try:
            result = self._client.search_site(keyword, page)
            
            albums = []
            for album_id, title in result.iter_id_title():
                if len(albums) >= limit:
                    break
                albums.append({
                    'id': album_id,
                    'title': title,
                })
            
            return {
                'results': albums,
                'total_pages': result.page_count,
                'current_page': page,
            }
        except Exception as e:
            logger.error(f"Search failed: {e}")
            return {'results': [], 'total_pages': 0, 'current_page': 1}
    
    def download_album(self, album_id: str, save_dir: str) -> List[str]:
        """
        下载本子到指定目录
        
        Returns:
            List[str]: 下载的图片路径列表
        """
        album_id = self._extract_id(album_id)
        os.makedirs(save_dir, exist_ok=True)
        
        # 获取本子详情
        album = self._client.get_album_detail(album_id)
        logger.info(f"Album: {album.title}")
        
        # 获取所有章节
        episodes = album.episode_list
        if not episodes:
            raise ValueError("No episodes found")
        
        # 只下载第一章（通常一个本子只有一章）
        first_episode = episodes[0]
        photo_id = first_episode[0]  # (id, index, title)
        logger.info(f"Downloading photo {photo_id}")
        
        # 获取章节详情
        photo = self._client.get_photo_detail(photo_id)
        
        # 下载所有图片
        image_paths = []
        for i, img_detail in enumerate(photo):
            try:
                # 生成文件名
                ext = os.path.splitext(img_detail.img_url)[1] if hasattr(img_detail, 'img_url') else '.webp'
                img_path = os.path.join(save_dir, f'{i+1:05d}{ext}')
                
                # 下载图片
                self._client.download_by_image_detail(img_detail, img_path)
                image_paths.append(img_path)
                logger.debug(f"Downloaded {i+1}/{len(photo)}")
            except Exception as e:
                logger.warning(f"Failed to download image {i+1}: {e}")
        
        logger.info(f"Downloaded {len(image_paths)} images")
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


_client = None


def get_jm_client(client_impl: str = 'api') -> JMApiClient:
    global _client
    if _client is None:
        _client = JMApiClient(client_impl)
    return _client


def is_available() -> bool:
    return JMCOMIC_AVAILABLE
