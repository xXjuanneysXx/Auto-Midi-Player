# -*- coding: utf-8 -*-
r"""
音游的判定图标 / 星星
====================

跟 uiicons.py 一个规矩，**用代码画**：不占打包体积、任意 DPI 都清晰、颜色跟着主题走。

判定牌长这样（新拟态 + 一点装饰，别方方正正的）：

* 一块软边胶囊牌（外阴影 + 上沿高光 + 判定色内描边 + 一层判定色光晕）；
* 文字用**斜体 + 微微上翘**，填色是渐变：
  PERFECT 直接用彩虹渐变，其它档用自己的色，从亮到更亮；
* 牌左边挂一个小装饰：PERFECT / COMBO 是五角星，GOOD 是四角闪光，
  WRONG 是闪电，MISS 是一道斜杠；
* PERFECT 底下还压一条彩虹细带 —— 一眼就认得出来。

画法先在「高度 = 1.0」的比例上比划，再按要的像素高度整体缩放 —— HUD 上 26 像素、
结算页 72 像素用的是同一套画法。
"""

import math


def _qt():
    try:
        from PySide6.QtCore import QPointF, QRectF, Qt
        from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics,
                                   QLinearGradient, QPainter, QPainterPath, QPen,
                                   QPixmap, QRadialGradient)
    except ImportError:                                   # 装了 PyQt6 也行
        from PyQt6.QtCore import QPointF, QRectF, Qt
        from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetrics,
                                 QLinearGradient, QPainter, QPainterPath, QPen,
                                 QPixmap, QRadialGradient)
    return dict(QPointF=QPointF, QRectF=QRectF, Qt=Qt, QBrush=QBrush, QColor=QColor,
                QFont=QFont, QFontMetrics=QFontMetrics,
                QLinearGradient=QLinearGradient, QPainter=QPainter,
                QPainterPath=QPainterPath, QPen=QPen, QPixmap=QPixmap,
                QRadialGradient=QRadialGradient)


# 判定色（跟程序现有配色一路：青 / 绿 / 灰 / 橙 / 红 / 金）
COLORS = {'perfect': '#3fd8e8', 'good': '#5fd18b', 'plain': '#8b93a7',
          'wrong': '#ffb347', 'miss': '#f08a8a', 'combo': '#ffc247'}

LABELS = {'perfect': 'PERFECT', 'good': 'GOOD', 'plain': 'MISS',
          'wrong': 'WRONG', 'miss': 'MISS', 'combo': 'COMBO'}

# 每个档位左边挂什么小装饰
DECOR = {'perfect': 'star', 'good': 'sparkle', 'combo': 'star',
         'wrong': 'bolt', 'miss': 'slash', 'plain': 'slash'}

# 彩虹（PERFECT 的文字和底下那条带子都用它）
RAINBOW = ('#ff6b6b', '#ffb347', '#ffc247', '#5fd18b', '#3fd8e8', '#7f8cff', '#e07bff')

FONTS = ('Microsoft YaHei UI', 'Segoe UI', 'Trebuchet MS', 'Verdana', 'Arial')
SCALE = 3.0                      # 先在 3 倍尺寸上画，缩下来更干净


def _font(q, size, weight=None, spacing=0.0, italic=True):
    font = q['QFont'](FONTS[0], 10)
    for family in FONTS:
        font.setFamily(family)
        break
    font.setPixelSize(max(6, int(round(size))))
    font.setWeight(weight or q['QFont'].Weight.Bold)
    font.setItalic(bool(italic))
    if spacing:
        font.setLetterSpacing(q['QFont'].SpacingType.AbsoluteSpacing, float(spacing))
    return font


def _sweep(q, rect, rainbow=False, color=None):
    """文字 / 装饰的填色：PERFECT 走彩虹，其它走「本色 → 本色提亮」。"""
    grad = q['QLinearGradient'](rect.left(), rect.top(), rect.right(), rect.bottom())
    if rainbow:
        for index, hue in enumerate(RAINBOW):
            grad.setColorAt(index / float(len(RAINBOW) - 1), q['QColor'](hue))
        return grad
    base = q['QColor'](color)
    grad.setColorAt(0.0, base.lighter(165))
    grad.setColorAt(0.45, base)
    grad.setColorAt(1.0, base.lighter(205))
    return grad


def _star_path(q, cx, cy, radius, ratio=0.45, points=5):
    """五角星（尖朝上）。"""
    path = q['QPainterPath']()
    for index in range(points * 2):
        angle = -math.pi / 2.0 + index * math.pi / points
        r = radius if index % 2 == 0 else radius * ratio
        point = q['QPointF'](cx + r * math.cos(angle), cy + r * math.sin(angle))
        if index == 0:
            path.moveTo(point)
        else:
            path.lineTo(point)
    path.closeSubpath()
    return path


def _sparkle_path(q, cx, cy, radius):
    """四角闪光（尖尖的）。"""
    path = q['QPainterPath']()
    inner = radius * 0.22
    path.moveTo(cx, cy - radius)
    path.quadTo(cx + inner, cy - inner, cx + radius, cy)
    path.quadTo(cx + inner, cy + inner, cx, cy + radius)
    path.quadTo(cx - inner, cy + inner, cx - radius, cy)
    path.quadTo(cx - inner, cy - inner, cx, cy - radius)
    path.closeSubpath()
    return path


def _bolt_path(q, cx, cy, radius):
    """闪电。"""
    path = q['QPainterPath']()
    path.moveTo(cx + radius * 0.30, cy - radius)
    path.lineTo(cx - radius * 0.55, cy + radius * 0.10)
    path.lineTo(cx - radius * 0.05, cy + radius * 0.10)
    path.lineTo(cx - radius * 0.28, cy + radius)
    path.lineTo(cx + radius * 0.55, cy - radius * 0.15)
    path.lineTo(cx + radius * 0.05, cy - radius * 0.15)
    path.closeSubpath()
    return path


def _decor_path(q, kind, cx, cy, radius):
    if kind == 'star':
        return _star_path(q, cx, cy, radius)
    if kind == 'sparkle':
        return _sparkle_path(q, cx, cy, radius)
    if kind == 'bolt':
        return _bolt_path(q, cx, cy, radius)
    return None


def badge(kind, height=30, text=None, extra='', width=None):
    """
    画一块判定牌（柔边胶囊 + 斜体渐变字 + 小装饰）。

    kind 见 COLORS；extra 是写在最前面的额外字（连击数就是 'x12'）；
    width=None 时按文字宽度自动撑开。
    """
    q = _qt()
    color = q['QColor'](COLORS.get(kind, COLORS['plain']))
    label = str(text if text is not None else LABELS.get(kind, kind)).strip()
    if extra:
        label = '%s %s' % (extra, label)
    h = max(11.0, float(height))
    font = _font(q, h * 0.44, spacing=h * 0.05)
    metrics = q['QFontMetrics'](font)
    text_w = float(metrics.horizontalAdvance(label))
    decor = DECOR.get(kind)
    decor_w = h * 0.62 if decor else 0.0          # 左边给小装饰留的位置
    w = float(width) if width else max(h * 3.0, text_w + decor_w + h * 1.35)

    big = int(w * SCALE)
    tall = int(h * SCALE)
    pixmap = q['QPixmap'](big, tall)
    pixmap.fill(q['Qt'].transparent)
    painter = q['QPainter'](pixmap)
    try:
        painter.setRenderHint(q['QPainter'].Antialiasing, True)
        painter.scale(SCALE, SCALE)
        plate = q['QRectF'](h * 0.06, h * 0.12, w - h * 0.12, h - h * 0.24)
        radius = plate.height() / 2.0
        painter.setPen(q['Qt'].NoPen)
        # 外阴影（往下压一点）
        painter.setBrush(q['QBrush'](q['QColor'](0, 0, 0, 140)))
        painter.drawRoundedRect(plate.adjusted(0, h * 0.07, 0, h * 0.07), radius, radius)
        # 上沿高光
        painter.setBrush(q['QBrush'](q['QColor'](255, 255, 255, 30)))
        painter.drawRoundedRect(plate.adjusted(h * 0.06, -h * 0.04, -h * 0.06, 0),
                                radius, radius)
        # 牌面
        base = q['QLinearGradient'](plate.topLeft(), plate.bottomRight())
        base.setColorAt(0.0, q['QColor']('#3a4664'))
        base.setColorAt(0.5, q['QColor']('#28324a'))
        base.setColorAt(1.0, q['QColor']('#1a2130'))
        painter.setBrush(q['QBrush'](base))
        painter.drawRoundedRect(plate, radius, radius)
        # 判定色光晕
        glow = q['QRadialGradient'](plate.center(), plate.width() * 0.58)
        glow.setColorAt(0.0, q['QColor'](color.red(), color.green(), color.blue(), 96))
        glow.setColorAt(1.0, q['QColor'](color.red(), color.green(), color.blue(), 0))
        painter.setBrush(q['QBrush'](glow))
        painter.drawRoundedRect(plate.adjusted(h * 0.05, h * 0.05, -h * 0.05, -h * 0.05),
                                radius * 0.9, radius * 0.9)
        # 内描边
        painter.setPen(q['QPen'](q['QColor'](color.red(), color.green(), color.blue(), 170),
                                 max(1.0, h * 0.05)))
        painter.setBrush(q['Qt'].NoBrush)
        painter.drawRoundedRect(plate.adjusted(h * 0.02, h * 0.02, -h * 0.02, -h * 0.02),
                                radius * 0.95, radius * 0.95)
        # PERFECT：牌底下压一条彩虹细带
        if kind == 'perfect':
            band = q['QRectF'](plate.left() + h * 0.16, plate.bottom() - h * 0.02,
                               plate.width() - h * 0.32, h * 0.10)
            painter.setPen(q['Qt'].NoPen)
            painter.setBrush(q['QBrush'](_sweep(q, band, rainbow=True)))
            painter.drawRoundedRect(band, band.height() / 2.0, band.height() / 2.0)
        # 文字（斜体 + 微微上翘）
        box = metrics.boundingRect(label)
        text_left = plate.left() + decor_w + (plate.width() - decor_w - text_w) / 2.0
        center_x = text_left + text_w / 2.0
        center_y = plate.center().y()
        path = q['QPainterPath']()
        path.addText(-box.width() / 2.0 - box.left(),
                     box.height() / 2.0 - box.bottom(), font, label)
        painter.save()
        painter.translate(center_x, center_y)
        painter.rotate(-5.0)
        painter.setPen(q['QPen'](q['QColor'](8, 11, 18, 210), max(1.2, h * 0.08),
                                 q['Qt'].PenStyle.SolidLine, q['Qt'].PenCapStyle.RoundCap,
                                 q['Qt'].PenJoinStyle.RoundJoin))
        painter.setBrush(q['Qt'].NoBrush)
        painter.drawPath(path)
        painter.setPen(q['Qt'].NoPen)
        painter.setBrush(q['QBrush'](_sweep(
            q, q['QRectF'](-text_w / 2.0, -h * 0.30, text_w, h * 0.60),
            rainbow=(kind == 'perfect'), color=color)))
        painter.drawPath(path)
        painter.restore()
        # 左边的小装饰
        if decor:
            cx = plate.left() + h * 0.16 + decor_w / 2.0
            cy = plate.center().y()
            radius = h * 0.22
            if decor == 'slash':
                painter.setPen(q['QPen'](q['QColor'](color.red(), color.green(),
                                                     color.blue(), 235),
                                         max(1.6, h * 0.10),
                                         q['Qt'].PenStyle.SolidLine,
                                         q['Qt'].PenCapStyle.RoundCap))
                painter.drawLine(q['QPointF'](cx - radius * 0.7, cy + radius * 0.9),
                                 q['QPointF'](cx + radius * 0.7, cy - radius * 0.9))
            else:
                shape = _decor_path(q, decor, cx, cy, radius)
                painter.setPen(q['QPen'](q['QColor'](8, 11, 18, 190),
                                         max(1.0, h * 0.05),
                                         q['Qt'].PenStyle.SolidLine,
                                         q['Qt'].PenCapStyle.RoundCap,
                                         q['Qt'].PenJoinStyle.RoundJoin))
                painter.setBrush(q['QBrush'](_sweep(
                    q, q['QRectF'](cx - radius, cy - radius, radius * 2, radius * 2),
                    rainbow=(kind == 'perfect'), color=color)))
                painter.drawPath(shape)
    finally:
        painter.end()
    pixmap.setDevicePixelRatio(SCALE)
    return pixmap


def star(size=22, rainbow=False, filled=True, alpha=255):
    """一颗五角星。rainbow=True 用彩带渐变（炫彩星），否则用金黄色。"""
    q = _qt()
    big = max(8, int(float(size) * SCALE))
    pixmap = q['QPixmap'](big, big)
    pixmap.fill(q['Qt'].transparent)
    painter = q['QPainter'](pixmap)
    try:
        painter.setRenderHint(q['QPainter'].Antialiasing, True)
        painter.scale(big / 100.0, big / 100.0)
        path = q['QPainterPath']()
        points = []
        for index in range(10):
            angle = -math.pi / 2.0 + index * math.pi / 5.0
            radius = 48.0 if index % 2 == 0 else 20.0
            points.append(q['QPointF'](50 + radius * math.cos(angle),
                                       52 + radius * math.sin(angle)))
        path.moveTo(points[0])
        for point in points[1:]:
            path.lineTo(point)
        path.closeSubpath()
        if filled:
            if rainbow:
                sweep = q['QLinearGradient'](0, 0, 100, 100)
                for stop, hue in ((0.0, '#ff6b6b'), (0.2, '#ffc247'), (0.4, '#5fd18b'),
                                  (0.6, '#3fd8e8'), (0.8, '#7f8cff'), (1.0, '#e07bff')):
                    sweep.setColorAt(stop, q['QColor'](hue))
                painter.setBrush(q['QBrush'](sweep))
                painter.setPen(q['QPen'](q['QColor'](255, 255, 255, 170), 4.0))
            else:
                painter.setBrush(q['QBrush'](q['QColor'](255, 194, 71, alpha)))
                painter.setPen(q['QPen'](q['QColor'](120, 78, 12, alpha), 4.0))
        else:
            painter.setBrush(q['Qt'].NoBrush)
            painter.setPen(q['QPen'](q['QColor'](255, 255, 255, 46), 5.0))
        painter.drawPath(path)
    finally:
        painter.end()
    pixmap.setDevicePixelRatio(SCALE)
    return pixmap
