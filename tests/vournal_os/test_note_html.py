"""Rich notes (design 2026-11-18): every HTML save is sanitized on the server against an allow-list,
and its plain text (contentText) feeds lists, search and the AI. Pure, no database."""

import unittest

from app.os.note_html import FONT_FAMILIES, html_text, markdown_text, sanitize


class Sanitize(unittest.TestCase):
    def test_strips_scripts_and_handlers(self):
        self.assertEqual(sanitize('<p onclick=x>a<script>b</script></p><img src=x onerror=y>'), "<p>a</p>")

    def test_javascript_and_data_links_dropped(self):
        self.assertEqual(sanitize('<a href="javascript:alert(1)">x</a>'), '<a rel="noopener noreferrer">x</a>')
        self.assertNotIn("data:", sanitize('<a href="data:text/html,hi">x</a>'))
        self.assertIn('target="_blank"', sanitize('<a href="https://vournal.app" target="_blank">x</a>'))
        self.assertNotIn("target", sanitize('<a href="https://vournal.app" target="evil">x</a>'))
        self.assertIn('href="mailto:a@b.co"', sanitize('<a href="mailto:a@b.co">m</a>'))

    def test_img_src_rules(self):
        self.assertIn('src="/api/attachments/abc_1-2/download"', sanitize('<img src="/api/attachments/abc_1-2/download">'))
        self.assertIn('src="/api/attachments/abc/download?space=w1"', sanitize('<img src="/api/attachments/abc/download?space=w1">'))
        for bad in ("data:image/png;base64,AAAA", "/api/attachments/abc/download?x=1", "//evil/x.png"):
            self.assertEqual(sanitize(f'<p>t</p><img src="{bad}">'), "<p>t</p>", bad)
        # a web image is never shown; it becomes a link to itself (final review)
        self.assertNotIn("<img", sanitize('<img src="https://evil/x.png">'))
        self.assertIn('width="50%"', sanitize('<img src="/api/attachments/a/download" width="50%">'))
        self.assertNotIn("width", sanitize('<img src="/api/attachments/a/download" width="999">'))

    def test_style_values(self):
        out = sanitize('<p style="color:#ff0000;font-size:18px;text-align:center;position:fixed;background:url(x)">t</p>')
        self.assertEqual(out, '<p style="color:#ff0000;font-size:18px;text-align:center">t</p>')
        self.assertEqual(sanitize('<span style="position:fixed">t</span>'), "<span>t</span>")
        self.assertIn("background-color:rgb(255, 255, 0)", sanitize('<mark style="background-color:rgb(255, 255, 0)">t</mark>'))
        self.assertNotIn("expression", sanitize('<span style="color:expression(alert(1))">t</span>'))

    def test_google_docs_paste(self):
        out = sanitize('<b style="font-weight:normal" id="docs-internal-guid-1"><span class="x" style="font-size:11pt;'
                       'font-family:Arial;color:#000000;font-weight:700">Hi</span></b><!-- c -->')
        self.assertIn("Hi", out)
        self.assertNotIn("color:#000000", out)  # documents' default black would vanish in dark mode
        self.assertIn(f"font-family:{FONT_FAMILIES['sans']}", out)
        self.assertIn("font-size:15px", out)
        for gone in ("id=", "class=", "font-weight", "<b", "<!--"):
            self.assertNotIn(gone, out, gone)
        self.assertNotIn("font-family", sanitize('<span style="font-family:Wingdings">t</span>'))

    def test_task_list_and_table_kept(self):
        task = '<ul data-type="taskList"><li data-type="taskItem" data-checked="true"><p>done</p></li></ul>'
        self.assertEqual(sanitize(task), task)
        table = '<table><tbody><tr><th colspan="2">h</th></tr><tr><td>1</td><td rowspan="1">2</td></tr></tbody></table>'
        self.assertEqual(sanitize(table), table)

    def test_arabic_text(self):
        out = sanitize('<p dir="auto">مرحبا بالعالم</p>')
        self.assertEqual(out, '<p dir="auto">مرحبا بالعالم</p>')
        self.assertEqual(html_text(out), "مرحبا بالعالم")
        self.assertNotIn("dir", sanitize('<p dir="sideways">x</p>'))


class ReviewFixes(unittest.TestCase):
    def test_default_colours_dropped(self):
        out = sanitize('<p style="color:#000;background-color:#ffffff">a</p><span style="color:rgb(0, 0, 0)">b</span>'
                       '<span style="color:#e5484d;background-color:transparent">c</span>')
        self.assertEqual(out, '<p>a</p><span>b</span><span style="color:#e5484d">c</span>')

    def test_foreign_images_become_links(self):
        self.assertEqual(sanitize('<img src="https://example.com/chart.png" alt="Q3 chart">'),
                         '<a href="https://example.com/chart.png" target="_blank" rel="noopener noreferrer">Q3 chart</a>')
        self.assertIn(">https://example.com/a.png</a>", sanitize('<img src="https://example.com/a.png">'))
        self.assertEqual(sanitize('<p>x</p><img src="javascript:alert(1)" alt="y">'), "<p>x</p>")
        self.assertNotIn("<script", sanitize('<img src="https://e.com/a.png" alt="<script>x</script>">'))


class Text(unittest.TestCase):
    def test_html_text_blocks_and_cells(self):
        html = "<h1>T</h1><p>a<br>b</p><ul><li>x</li><li>y</li></ul><table><tr><td>1</td><td>2</td></tr></table>"
        self.assertEqual(html_text(html), "T\na\nb\nx\ny\n1\t2")

    def test_html_text_entities_and_spaces(self):
        self.assertEqual(html_text("<p>a &amp;   b</p><p></p><p></p><p></p><p>c</p>"), "a & b\n\nc")

    def test_markdown_text_keeps_lines(self):
        self.assertEqual(markdown_text("# Title\n\n- **one**\n- [x] two\n\nSee [docs](https://x.y)"), "Title\n\none\ntwo\n\nSee docs")
