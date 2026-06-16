"""
PDF生成模块
使用 img2pdf 实现高效图片转PDF
"""

import os
import logging
from typing import List
import img2pdf
from pypdf import PdfWriter, PdfReader

logger = logging.getLogger(__name__)

# 每批处理的图片数量
BATCH_SIZE = 20


class PDFMaker:
    """PDF生成器"""
    
    @staticmethod
    def images_to_pdf(image_paths: List[str], output_path: str, title: str = "") -> str:
        """
        将多张图片合并为PDF
        
        使用 img2pdf 实现：
        - 无损转换（不重新编码JPEG）
        - 文件小（仅PDF容器开销）
        - 速度快（不处理像素）
        
        Args:
            image_paths: 图片文件路径列表
            output_path: PDF输出路径
            title: PDF标题（可选）
            
        Returns:
            str: 生成的PDF文件路径
        """
        if not image_paths:
            raise ValueError("No images provided")
        
        # 过滤存在的图片
        valid_paths = [p for p in image_paths if os.path.exists(p)]
        
        if not valid_paths:
            raise ValueError("No valid images found")
        
        logger.info(f"Converting {len(valid_paths)} images to PDF")
        
        # img2pdf 直接转换
        with open(output_path, "wb") as f:
            f.write(img2pdf.convert(valid_paths))
        
        logger.info(f"Created PDF: {output_path}")
        return output_path
    
    @staticmethod
    def get_pdf_size(pdf_path: str) -> int:
        """获取PDF文件大小（字节）"""
        if os.path.exists(pdf_path):
            return os.path.getsize(pdf_path)
        return 0
    
    @staticmethod
    def format_size(size_bytes: int) -> str:
        """格式化文件大小"""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024.0:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.1f} TB"
