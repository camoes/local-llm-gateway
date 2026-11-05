import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_driver as ad  # noqa: E402


class TestUnwrap(unittest.TestCase):
    def test_bing_url_envuelta(self):
        # a1 + base64url de https://www.somenergia.coop/es
        href = "https://www.bing.com/ck/a?!&&p=abc&u=a1aHR0cHM6Ly93d3cuc29tZW5lcmdpYS5jb29wL2Vz&ntb=1"
        self.assertEqual(ad._unwrap_bing(href), "https://www.somenergia.coop/es")

    def test_bing_sin_wrapper(self):
        self.assertEqual(ad._unwrap_bing("https://ejemplo.com/x"), "https://ejemplo.com/x")

    def test_bing_url_invalida(self):
        self.assertEqual(ad._unwrap_bing("https://www.bing.com/ck/a?!&&p=abc&u=zz"), "")

    def test_ddg_uddg(self):
        href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fejemplo.com%2Fa&rut=x"
        self.assertEqual(ad._unwrap_ddg(href), "https://ejemplo.com/a")

    def test_ddg_anuncio(self):
        href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fduckduckgo.com%2Fy.js%3Fx"
        self.assertEqual(ad._unwrap_ddg(href), "")


class TestParseBing(unittest.TestCase):
    def test_extrae_titulo_url_y_extracto(self):
        html = ('<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9lamVtcGxvLmNvbS9h">'
                'Titulo &amp; mas</a></h2><p class="b_lineclamp">Extracto de prueba</p></li>')
        tablas = ad.re.findall(r'<li class="b_algo".*?</li>', html, ad.re.S)
        self.assertEqual(len(tablas), 1)
        m = ad.re.search(r'<h2>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', tablas[0], ad.re.S)
        self.assertEqual(ad._unwrap_bing(m.group(1)), "https://ejemplo.com/a")


class TestContexto(unittest.TestCase):
    def test_documento_simple(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "nota.txt"
            p.write_text("Contenido de prueba suficientemente largo para pasar el filtro.", encoding="utf-8")
            texto = ad.load_context(str(p))
            self.assertIn("nota.txt", texto)
            self.assertIn("Contenido de prueba", texto)


if __name__ == "__main__":
    unittest.main(verbosity=2)
