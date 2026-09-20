# -*- coding: utf-8 -*-
"""
程序图标：圆角蓝底 + 一个音符。

用矢量画的，不依赖字体 —— 之前拿 QFont 画「♪」，在没有字体的环境里
（比如打包机上跑 offscreen）会画成一个空方块。

主程序、安装程序、快捷方式的图标都从这里出，保证是同一个样子。
"""

try:                                                  # 优先 Qt 官方绑定
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import (QBrush, QColor, QIcon, QPainter, QPainterPath,
                               QPen, QPixmap)
except ImportError:                                   # 装了 PyQt6 也行
    from PyQt6.QtCore import QPointF, QRectF, Qt
    from PyQt6.QtGui import (QBrush, QColor, QIcon, QPainter, QPainterPath,
                             QPen, QPixmap)


import theme


BACKGROUND = theme.c('#3b82f6')
NOTE = theme.c('#ffffff')
# 图标按 64×64 设计，别的尺寸等比缩放
SIZES = (16, 24, 32, 48, 64, 128, 256)


def note_pixmap(size=64):
    """画一个 size×size 的图标。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    scale = size / 64.0

    # 圆角底
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(BACKGROUND)))
    painter.drawRoundedRect(QRectF(2 * scale, 2 * scale, 60 * scale, 60 * scale),
                            16 * scale, 16 * scale)

    # 音符：斜着的符头 + 符干 + 符尾
    painter.setBrush(QBrush(QColor(NOTE)))
    painter.save()
    painter.translate(27 * scale, 44.5 * scale)
    painter.rotate(-18)
    painter.drawEllipse(QPointF(0, 0), 10.5 * scale, 8.2 * scale)
    painter.restore()
    painter.drawRect(QRectF(36.4 * scale, 15.5 * scale, 3.0 * scale, 29 * scale))
    flag = QPainterPath()
    flag.moveTo(38.0 * scale, 16.0 * scale)
    flag.cubicTo(50 * scale, 21 * scale, 52.5 * scale, 30 * scale, 45.5 * scale, 36.5 * scale)
    flag.cubicTo(48 * scale, 28 * scale, 44 * scale, 23 * scale, 38.0 * scale, 23.0 * scale)
    flag.closeSubpath()
    painter.drawPath(flag)
    painter.end()
    return pixmap


def make_icon():
    """给窗口 / exe 用的 QIcon：多带几个尺寸，任务栏和资源管理器都清楚。"""
    icon = QIcon()
    for size in SIZES:
        icon.addPixmap(note_pixmap(size))
    return icon