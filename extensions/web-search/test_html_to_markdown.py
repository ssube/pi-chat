import subprocess
import sys
import unittest
from pathlib import Path
from html_to_markdown import convert


class HtmlMarkdownTests(unittest.TestCase):
    def test_keeps_article_title_inside_header(self):
        self.assertEqual(convert('<article><header><h1>Story</h1><nav>Menu</nav></header><p>Body</p></article>'), '# Story\n\nBody')

    def test_removes_navigation_controls_ads_and_keeps_structure(self):
        html = '''<html><head><title>Article</title></head><body>
        <nav>Buy things</nav><main><header>Site menu</header>
        <h1>Article</h1><p>Useful <strong>text</strong> with <a href="https://example.com/source">source</a>.</p>
        <div class="sidebar">Noise</div><button>Click</button><div id="ad-banner">Ad</div>
        <h2>Items</h2><ul><li>One</li><li>Two</li></ul>
        <figure><img src="/diagrams/flow.png" alt="Flow chart"></figure>
        <table><tr><th>Name</th><th>Value</th></tr><tr><td>Alpha</td><td>1</td></tr></table>
        <script>secretScript()</script></main><footer>Footer</footer></body></html>'''
        result = convert(html, "https://example.com/article")
        for kept in ('# Article', 'Useful **text**', '[source](https://example.com/source)',
                     '## Items', '- One', '- Two', '![Flow chart](https://example.com/diagrams/flow.png)', '| Name | Value |', '| --- | --- |', '| Alpha | 1 |'):
            self.assertIn(kept, result)
        for removed in ('Buy things', 'Site menu', 'Noise', 'Click', 'Ad', 'secretScript', 'Footer'):
            self.assertNotIn(removed, result)

    def test_cli_preserves_long_pages(self):
        html = '<main><h1>Long page</h1>' + '<p>' + 'text ' * 60000 + '</p></main>'
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('html_to_markdown.py'))],
                                input=html, text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout, convert(html) + '\n')
        self.assertGreater(len(result.stdout), 200_000)


if __name__ == '__main__':
    unittest.main()
