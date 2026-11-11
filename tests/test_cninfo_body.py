"""cninfo PDF 正文提取（v2）：注入固定 PDF 字节，绝不真打网。"""
from __future__ import annotations

import io

from pypdf import PdfWriter

from fd_info.fetchers.cninfo import fetch_body


def _pdf_bytes(text: str) -> bytes:
    # pypdf 的 writer 只能写空白页；用最小合法 PDF 手写含文本对象
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for o in offsets:
        out.write(b"%010d 00000 n \n" % o)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    return out.getvalue()


def test_extracts_text_from_pdf():
    body = fetch_body("https://example.test/a.PDF", download=lambda url: _pdf_bytes("Hello CNINFO body"))
    assert body is not None
    assert "Hello CNINFO body" in body


def test_download_failure_returns_none():
    def boom(url: str) -> bytes:
        raise RuntimeError("network down")

    assert fetch_body("https://example.test/a.PDF", download=boom) is None


def test_empty_pdf_returns_none():
    assert fetch_body("https://example.test/a.PDF", download=lambda url: _pdf_bytes("   ")) is None


def test_blank_url_returns_none():
    assert fetch_body("") is None


def test_detailed_classifies_no_text_layer():
    """扫描/图片型 PDF（下载成功、零字符）→ no_text（重试无意义）。"""
    from fd_info.fetchers.cninfo import fetch_body_detailed

    body, status = fetch_body_detailed("https://example.test/a.PDF", download=lambda u: _pdf_bytes("   "))
    assert body is None and status == "no_text"


def test_detailed_classifies_dead_link():
    from fd_info.fetchers.cninfo import fetch_body_detailed

    def boom(u: str) -> bytes:
        raise RuntimeError("404")

    body, status = fetch_body_detailed("https://example.test/a.PDF", download=boom)
    assert body is None and status == "failed"


def test_detailed_classifies_stored():
    from fd_info.fetchers.cninfo import fetch_body_detailed

    body, status = fetch_body_detailed("https://example.test/a.PDF",
                                       download=lambda u: _pdf_bytes("Scanme text"))
    assert status == "stored" and body and "Scanme" in body


def test_detailed_classifies_oversize():
    """超单份上限 → oversize（终态）。生产实证 2026-11-11：512Mi 容器被一份
    大 PDF 打穿整轮回填，而 OOM 时日志全空 —— 上限必须显式且可分类落库。"""
    from fd_info.fetchers.cninfo import PdfTooLarge, fetch_body_detailed

    def too_big(u: str) -> bytes:
        raise PdfTooLarge(999_999_999)

    body, status = fetch_body_detailed("https://example.test/a.PDF", download=too_big)
    assert body is None and status == "oversize"


def test_download_rejects_on_content_length_without_reading():
    """声明体积超限 → 立刻拒（不把整包下完再判）。"""
    import httpx

    from fd_info.fetchers.cninfo import PdfTooLarge, _download_pdf

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "2000"}, content=b"x" * 10)

    try:
        _download_pdf("https://example.test/big.PDF", max_bytes=1000,
                      transport=httpx.MockTransport(handler))
    except PdfTooLarge as exc:
        assert exc.size == 2000
    else:
        raise AssertionError("应抛 PdfTooLarge")


def test_download_rejects_while_streaming_when_length_absent():
    """无 Content-Length 时边下边算，超限即断 —— 防 chunked 大包吃满内存。"""
    import httpx

    from fd_info.fetchers.cninfo import PdfTooLarge, _download_pdf

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=iter([b"x" * 600, b"x" * 600]))

    try:
        _download_pdf("https://example.test/chunked.PDF", max_bytes=1000,
                      transport=httpx.MockTransport(handler))
    except PdfTooLarge as exc:
        assert exc.size > 1000
    else:
        raise AssertionError("应抛 PdfTooLarge")


def test_download_under_cap_returns_exact_bytes():
    import httpx

    from fd_info.fetchers.cninfo import _download_pdf

    payload = _pdf_bytes("under cap")
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    assert _download_pdf("https://example.test/ok.PDF", max_bytes=10_000_000,
                         transport=httpx.MockTransport(handler)) == payload
