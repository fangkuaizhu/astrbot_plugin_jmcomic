"""
PDF生成模块
使用 img2pdf 实现高效图片转PDF
"""

import os
import logging
from typing import List
import img2pdf

logger = logging.getLogger(__name__)


class PDFMaker:
    """PDF生成器"""
    
    @staticmethod
    def images_to_pdf(image_paths: List[str], output_path: str, title: str = "") -> str:
        """
        将多张图片合并为PDF
        
        Args:
            image_paths: 图片文件路径列表
            output_path: PDF输出路径
            title: PDF标题（可选）
            
        Returns:
            str: 生成的PDF文件路径
        """
        if not image_paths:
            raise ValueError("No images provided")
        
        valid_paths = [p for p in image_paths if os.path.exists(p)]
        if not valid_paths:
            raise ValueError("No valid images found")
        
        logger.info(f"Converting {len(valid_paths)} images to PDF")
        
        with open(output_path, "wb") as f:
            f.write(img2pdf.convert(valid_paths))
        
        logger.info(f"Created PDF: {output_path}")
        return output_path
