# -*- coding: utf-8 -*-
r"""
自绘图标
========

顶部那几个按钮（公告 / 更新 / B站 / GitHub）的图标，跟 noteicon.py 一样**用代码画**：

* 不用带图片资源 —— 打包体积不变，也不怕找不到文件；
* 任意 DPI 都清晰（先在 3 倍尺寸上画，再交给 Qt 缩放）；
* 颜色跟着主题走（换主题重新画一遍就行）；
* 不搬别人现成的图标文件，自己画个意思到了的简化图形。

画法统一在 24x24 的「设计画布」里比划，再按想要的大小整体缩放，
所有图形都用 Qt.NoPen + 填充（或圆头笔帽）画，小尺寸下不会糊成一团。
"""


def _qt():
    try:
        from PySide6.QtCore import QPointF, QRectF, Qt
        from PySide6.QtGui import (QBrush, QColor, QIcon, QPainter, QPainterPath,
                                   QPen, QPixmap, QPolygonF)
    except ImportError:                                   # 装了 PyQt6 也行
        from PyQt6.QtCore import QPointF, QRectF, Qt
        from PyQt6.QtGui import (QBrush, QColor, QIcon, QPainter, QPainterPath,
                                 QPen, QPixmap, QPolygonF)
    return dict(QPointF=QPointF, QRectF=QRectF, Qt=Qt, QBrush=QBrush, QColor=QColor,
                QIcon=QIcon, QPainter=QPainter, QPainterPath=QPainterPath, QPen=QPen,
                QPixmap=QPixmap, QPolygonF=QPolygonF)


# ---------- 四个图形 ----------

def _draw_announce(p, q, color):
    """公告：一个小铃铛。"""
    path = q['QPainterPath']()
    path.moveTo(6.4, 15.4)
    path.lineTo(6.4, 11.2)
    path.arcTo(q['QRectF'](6.4, 5.2, 11.2, 11.2), 180.0, -180.0)
    path.lineTo(17.6, 15.4)
    path.closeSubpath()
    p.setPen(q['Qt'].NoPen)
    p.setBrush(q['QBrush'](color))
    p.drawPath(path)
    # 顶上那个小提手
    p.drawRoundedRect(q['QRectF'](10.7, 2.9, 2.6, 2.6), 1.0, 1.0)
    # 底下那个小锤
    p.drawEllipse(q['QPointF'](12.0, 18.0), 1.9, 1.9)


def _draw_update(p, q, color):
    """更新：一个循环箭头。"""
    pen = q['QPen'](color, 2.1)
    pen.setCapStyle(q['Qt'].RoundCap)
    p.setPen(pen)
    p.setBrush(q['Qt'].NoBrush)
    path = q['QPainterPath']()
    path.arcMoveTo(q['QRectF'](4.6, 4.6, 14.8, 14.8), 62.0)
    path.arcTo(q['QRectF'](4.6, 4.6, 14.8, 14.8), 62.0, -300.0)
    p.drawPath(path)
    # 箭头
    p.setPen(q['Qt'].NoPen)
    p.setBrush(q['QBrush'](color))
    p.drawPolygon(q['QPolygonF']([
        q['QPointF'](16.2, 3.2), q['QPointF'](19.6, 4.6), q['QPointF'](16.4, 7.0)]))


def _draw_bilibili(p, q, color):
    """哔哩哔哩：一台小电视（两根天线 + 圆角屏幕 + 两只眼睛）。"""
    pen = q['QPen'](color, 2.0)
    pen.setCapStyle(q['Qt'].RoundCap)
    p.setPen(pen)
    p.setBrush(q['Qt'].NoBrush)
    p.drawLine(q['QPointF'](8.2, 6.4), q['QPointF'](6.0, 3.6))
    p.drawLine(q['QPointF'](15.8, 6.4), q['QPointF'](18.0, 3.6))
    p.setPen(q['Qt'].NoPen)
    p.setBrush(q['QBrush'](color))
    p.drawRoundedRect(q['QRectF'](3.2, 6.4, 17.6, 13.4), 3.4, 3.4)
    # 眼睛抠成底色（画两段竖线）
    pen2 = q['QPen'](q['QColor'](_contrast_hint(p)), 1.9)
    pen2.setCapStyle(q['Qt'].RoundCap)
    p.setPen(pen2)
    p.drawLine(q['QPointF'](9.0, 11.4), q['QPointF'](9.0, 14.4))
    p.drawLine(q['QPointF'](15.0, 11.4), q['QPointF'](15.0, 14.4))


def _draw_github(p, q, color):
    """GitHub 仓库：一只简化的小猫脑袋（两只耳朵 + 眼睛 + 鼻子）。"""
    p.setPen(q['Qt'].NoPen)
    p.setBrush(q['QBrush'](color))
    # 耳朵
    p.drawPolygon(q['QPolygonF']([
        q['QPointF'](5.6, 9.2), q['QPointF'](7.2, 3.2), q['QPointF'](10.4, 6.8)]) )
    p.drawPolygon(q['QPolygonF']([
        q['QPointF'](18.4, 9.2), q['QPointF'](16.8, 3.2), q['QPointF'](13.6, 6.8)]) )
    # 脸
    p.drawEllipse(q['QPointF'](12.0, 13.2), 7.2, 6.6)
    # 眼睛 / 鼻子：用透明的洞挖出来（先画一块和背景无关的透明，再叠颜色是不行的，
    # 所以这里直接把眼睛画成「深色」——由调用方给的底色决定对比）
    pen = q['QPen'](q['QColor'](_contrast_hint(p)), 1.7)
    pen.setCapStyle(q['Qt'].RoundCap)
    p.setPen(pen)
    p.drawPoint(q['QPointF'](9.6, 12.4))
    p.drawPoint(q['QPointF'](14.4, 12.4))
    p.drawLine(q['QPointF'](11.0, 16.0), q['QPointF'](12.0, 16.8))
    p.drawLine(q['QPointF'](13.0, 16.0), q['QPointF'](12.0, 16.8))


def _draw_reward(p, q, color):
    """打赏：一颗心（最简单也最好认）。"""
    p.setPen(q['Qt'].NoPen)
    p.setBrush(q['QBrush'](color))
    path = q['QPainterPath']()
    path.moveTo(12.0, 19.6)
    path.cubicTo(4.2, 14.4, 2.6, 10.4, 4.6, 7.2)
    path.cubicTo(6.5, 4.3, 10.2, 4.7, 12.0, 7.6)
    path.cubicTo(13.8, 4.7, 17.5, 4.3, 19.4, 7.2)
    path.cubicTo(21.4, 10.4, 19.8, 14.4, 12.0, 19.6)
    path.closeSubpath()
    p.drawPath(path)


def _draw_search(p, q, color):
    """搜索：一个放大镜（圆 + 手柄）。"""
    pen = q['QPen'](color, 2.1)
    pen.setCapStyle(q['Qt'].RoundCap)
    p.setPen(pen)
    p.setBrush(q['Qt'].NoBrush)
    p.drawEllipse(q['QPointF'](10.6, 10.6), 5.4, 5.4)
    p.drawLine(q['QPointF'](14.7, 14.7), q['QPointF'](19.6, 19.6))


def _draw_quickstart(p, q, color):
    """快速上手：一本翻开的小书 + 一枚问号（「怎么用？」）。"""
    pen = q['QPen'](color, 1.9)
    pen.setCapStyle(q['Qt'].RoundCap)
    pen.setJoinStyle(q['Qt'].RoundJoin)
    p.setPen(pen)
    p.setBrush(q['Qt'].NoBrush)
    path = q['QPainterPath']()                  # 摊开的两页
    path.moveTo(3.4, 5.6)
    path.cubicTo(6.0, 4.2, 9.0, 4.2, 11.2, 6.0)
    path.cubicTo(13.4, 4.2, 16.4, 4.2, 19.0, 5.6)
    path.lineTo(19.0, 17.0)
    path.cubicTo(16.4, 15.6, 13.4, 15.6, 11.2, 17.4)
    path.cubicTo(9.0, 15.6, 6.0, 15.6, 3.4, 17.0)
    path.closeSubpath()
    p.drawPath(path)
    p.drawLine(q['QPointF'](11.2, 6.0), q['QPointF'](11.2, 17.4))
    pen2 = q['QPen'](q['QColor'](_contrast_hint(p)), 1.7)
    pen2.setCapStyle(q['Qt'].RoundCap)
    p.setPen(pen2)
    p.setBrush(q['QBrush'](q['QColor'](_contrast_hint(p))))
    p.drawEllipse(q['QPointF'](15.4, 11.2), 3.6, 3.6)       # 挖个底色的圆当问号的底
    p.setPen(q['QPen'](color, 1.5))
    p.setBrush(q['Qt'].NoBrush)
    path2 = q['QPainterPath']()
    path2.moveTo(14.0, 10.1)
    path2.cubicTo(14.1, 9.0, 16.7, 9.0, 16.7, 10.4)
    path2.cubicTo(16.7, 11.3, 15.4, 11.3, 15.4, 12.3)
    p.drawPath(path2)
    p.setPen(q['Qt'].NoPen)
    p.setBrush(q['QBrush'](color))
    p.drawEllipse(q['QPointF'](15.4, 13.6), 0.9, 0.9)


# 眼睛那种「挖洞」没法真的挖（图标是透明的），所以拿一个和主色反着来的颜色顶上
_hint = {'color': '#ffffff'}


def _contrast_hint(_painter):
    return _hint['color']


def _set_hint(color):
    """画图标之前告诉它「按钮的底色是什么」，眼睛 / 鼻子就画成这个色（看着像挖空）。"""
    _hint['color'] = str(color or '#0f1219')


# ---------- 对外 ----------

DRAWERS = {
    'announce': _draw_announce,
    'update': _draw_update,
    'bilibili': _draw_bilibili,
    'github': _draw_github,
    'reward': _draw_reward,
    'search': _draw_search,
    'quickstart': _draw_quickstart,
}

SCALE = 3.0                     # 先在 3 倍尺寸上画，缩下来更干净


def make_icon(name, color, size=18, dot=False, dot_color='#ff5c5c', bg=None):
    """
    画一个图标。name 见 DRAWERS；color 是线条 / 填充色；bg 是按钮底色（眼睛 / 鼻子
    画成它，看着像挖空）；dot=True 时右上角点一个小红点（公告有没看过的就用它）。
    返回 QIcon。
    """
    q = _qt()
    _set_hint(bg or '#0f1219')
    big = int(max(8.0, float(size) * SCALE))
    pixmap = q['QPixmap'](big, big)
    pixmap.fill(q['Qt'].transparent)
    painter = q['QPainter'](pixmap)
    try:
        painter.setRenderHint(q['QPainter'].Antialiasing, True)
        painter.scale(big / 24.0, big / 24.0)
        drawer = DRAWERS.get(name)
        if drawer is not None:
            drawer(painter, q, q['QColor'](color))
        if dot:
            painter.setPen(q['Qt'].NoPen)
            painter.setBrush(q['QBrush'](q['QColor'](dot_color)))
            painter.drawEllipse(q['QPointF'](19.4, 4.6), 3.4, 3.4)
    finally:
        painter.end()
    pixmap.setDevicePixelRatio(SCALE)
    return q['QIcon'](pixmap)


def icon_color_for_button():
    """图标默认用哪个颜色（跟主题里的小圆钮文字色一致）。"""
    import theme
    return theme.c('#b9c1d1')
