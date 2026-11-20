# -*- coding: utf-8 -*-
"""Tạo PDF test: trang 1 = text vector tiếng Việt, trang 2 = ảnh (giả lập scan)."""
import os
import sys
import AppKit
from Foundation import NSMakePoint, NSMakeRect
from Quartz import (CGPDFContextCreateWithURL, CGPDFContextBeginPage,
                    CGPDFContextEndPage, CGRectMake, CGSizeMake, NSURL)

W, H = 595.0, 842.0  # A4 pt

TEXT = (
    "HỢP ĐỒNG THUÊ NHÀ\n"
    "Bên A: Nguyễn Văn An — CMND 012345678\n"
    "Bên B: Trần Thị Bình — CMND 987654321\n"
    "Giá thuê: 8.500.000 đồng/tháng, thanh toán trước ngày 05 hàng tháng.\n"
    "Thời hạn thuê 24 tháng kể từ ngày ký hợp đồng.\n"
    "Điều 5: Bên B không được cải tạo nhà khi chưa có sự đồng ý bằng văn bản của Bên A.\n"
    "Mọi tranh chấp sẽ được giải quyết tại Tòa án nhân dân quận Cầu Giấy, Hà Nội."
)


def draw_page(ctx, as_image: bool):
    CGPDFContextBeginPage(ctx, None)
    g = AppKit.NSGraphicsContext.graphicsContextWithCGContext_flipped_(ctx, False)
    AppKit.NSGraphicsContext.setCurrentContext_(g)
    font = AppKit.NSFont.systemFontOfSize_(13)
    from AppKit import NSAttributedString as NSA
    astr = NSA.alloc().initWithString_attributes_(TEXT, {"NSFont": font})
    if as_image:
        # vẽ text vào ảnh trước (mất text layer) rồi dán ảnh vào trang -> giả lập scan
        img = AppKit.NSImage.alloc().initWithSize_(CGSizeMake(W, H))
        img.lockFocus()
        AppKit.NSColor.whiteColor().set()
        AppKit.NSBezierPath.bezierPathWithRect_(NSMakeRect(0, 0, W, H)).fill()
        astr.drawAtPoint_(NSMakePoint(60, H - 150))
        img.unlockFocus()
        img.drawInRect_fromRect_operation_fraction_(
            NSMakeRect(0, 0, W, H), NSMakeRect(0, 0, W, H),
            AppKit.NSCompositingOperationCopy, 1.0)
    else:
        astr.drawInRect_(NSMakeRect(50, H - 400, W - 100, 380))
    CGPDFContextEndPage(ctx)


def main(out: str):
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    ctx = CGPDFContextCreateWithURL(NSURL.fileURLWithPath_(out),
                                    CGRectMake(0, 0, W, H), None)
    assert ctx is not None, 'KHONG tao duoc PDF context (thu muc chua ton tai?)'
    draw_page(ctx, as_image=False)
    draw_page(ctx, as_image=True)
    from Quartz import CGPDFContextClose
    CGPDFContextClose(ctx)  # finalize: ghi xref table


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "test.pdf")
    print("Tạo xong:", sys.argv[1] if len(sys.argv) > 1 else "test.pdf")
