from flask import Flask, request, jsonify, render_template, send_file
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
import io
import os
import re
import urllib.parse
import math
import hashlib
import json

app = Flask(__name__)


class PaulinaScraper:
    """
    Scraper para paulinamayorista.com.ar (sitio NUEVO, 2026).

    El sitio nuevo se renderiza con JavaScript, así que los datos del producto
    ya no están en inputs/tablas HTML sino en:
      - metaetiquetas og:title / og:image (nombre e imagen)
      - JSON embebido en <script> (JSON-LD, __NEXT_DATA__, payload RSC de Next.js,
        __NUXT_DATA__, window.__ESTADO__ = {...}, etc.) → precio, talles y colores
    Se prueban varias estrategias en cascada y, como último recurso, la
    lógica del sitio VIEJO (inputs + tabla) para URLs productoparticular.php.
    """

    NAME_KEYS = ('nombre', 'name', 'titulo', 'title', 'descripcion', 'description', 'producto')
    PRICE_KEYS_PREFERRED = (
        'precio', 'price', 'precioventa', 'precio_venta', 'preciofinal', 'precio_final',
        'finalprice', 'saleprice', 'precio_mayorista', 'preciomayorista', 'precio_unitario',
        'preciounitario', 'unitprice', 'amount', 'valor'
    )
    PRICE_KEYS_EXCLUDED = ('anterior', 'old', 'compare', 'tachado', 'lista', 'antes',
                           'original', 'min', 'max', 'costo', 'cost', 'currency', 'moneda')
    SIZE_KEYS = ('talle', 'talles', 'size', 'sizes', 'talla', 'tallas', 'medida', 'medidas')
    COLOR_KEYS = ('color', 'colores', 'colour', 'colors', 'colours')
    STOCK_KEYS = ('stock', 'cantidad', 'disponible', 'available', 'existencia', 'existencias',
                  'inventory', 'qty', 'quantity', 'stock_disponible', 'stockdisponible',
                  'instock', 'in_stock', 'hay_stock', 'haystock')

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                           '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'es-AR,es;q=0.9,en;q=0.8',
        })

    # ------------------------------------------------------------------ #
    # Punto de entrada
    # ------------------------------------------------------------------ #
    def scrape_product(self, url, debug=False):
        try:
            url = url.strip()
            print(f"🔍 Scraping URL: {url}")
            response = self.session.get(url, timeout=20)
            response.raise_for_status()
            html = response.text
            soup = BeautifulSoup(html, 'html.parser')

            blobs = self.extract_embedded_json(soup)
            print(f"🧩 Bloques JSON embebidos encontrados: {len(blobs)}")

            product_obj, ld_product = self.find_product_object(blobs, soup, url)

            product_data = {
                'name': self.extract_name(soup, product_obj, ld_product),
                'price': self.extract_price(soup, product_obj, ld_product, blobs),
                'image_url': self.extract_image(soup, url, product_obj, ld_product,
                                                html, blobs, self.product_code(soup, url)),
                'sizes_colors': self.extract_sizes_and_colors(soup, product_obj, blobs, self.product_code(soup, url)),
                'original_url': url
            }

            if debug:
                product_data['diagnostico'] = self.build_diagnostics(html, soup, blobs, product_obj,
                                                                     self.product_code(soup, url))

            print(f"✅ Datos extraídos: { {k: v for k, v in product_data.items() if k != 'diagnostico'} }")
            return product_data

        except Exception as e:
            print(f"❌ Error en scraping: {e}")
            return {'error': str(e)}

    # ------------------------------------------------------------------ #
    # JSON embebido
    # ------------------------------------------------------------------ #
    def extract_embedded_json(self, soup):
        """Devuelve una lista de (origen, objeto_python) con todo el JSON encontrado."""
        blobs = []
        decoder = json.JSONDecoder()

        # 1) JSON-LD (schema.org)
        for s in soup.find_all('script', attrs={'type': 'application/ld+json'}):
            try:
                blobs.append(('ld+json', json.loads(s.string or s.get_text())))
            except Exception:
                pass

        # 2) Next.js (pages router)
        nd = soup.find('script', id='__NEXT_DATA__')
        if nd:
            try:
                blobs.append(('__NEXT_DATA__', json.loads(nd.string or nd.get_text())))
            except Exception:
                pass

        # 3) Nuxt 3 (formato "devalue")
        nx = soup.find('script', id='__NUXT_DATA__')
        if nx:
            try:
                blobs.append(('__NUXT_DATA__', self.devalue_unflatten(json.loads(nx.string or nx.get_text()))))
            except Exception:
                pass

        # 4) Next.js App Router: self.__next_f.push([1,"..."])
        rsc_text = []
        other_scripts = []
        for s in soup.find_all('script'):
            if s.get('src') or s.get('type') in ('application/ld+json',):
                continue
            txt = s.string or s.get_text() or ''
            if '__next_f' in txt:
                for m in re.finditer(r'self\.__next_f\.push\(\[\s*1\s*,\s*("(?:[^"\\]|\\.)*")\s*\]\)', txt, re.S):
                    try:
                        rsc_text.append(json.loads(m.group(1)))
                    except Exception:
                        pass
            elif txt.strip():
                other_scripts.append(txt)

        texts = []
        if rsc_text:
            texts.append(('rsc', ''.join(rsc_text)))
        for t in other_scripts:
            texts.append(('script', t))

        # 5) Escanear textos buscando objetos/arrays JSON válidos
        for origin, text in texts:
            if len(text) > 3_000_000:
                text = text[:3_000_000]
            i = 0
            n = len(text)
            while i < n:
                j = text.find('{"', i)
                k = text.find('[{"', i)
                cands = [p for p in (j, k) if p != -1]
                if not cands:
                    break
                pos = min(cands)
                try:
                    obj, end = decoder.raw_decode(text, pos)
                    if isinstance(obj, (dict, list)) and len(json.dumps(obj)) > 20:
                        blobs.append((origin, obj))
                    i = end
                except Exception:
                    i = pos + 1

        return blobs

    def devalue_unflatten(self, data):
        """Reconstruye el formato 'devalue' que usa Nuxt 3."""
        if not isinstance(data, list):
            return data
        cache = {}
        wrappers = {'Reactive', 'ShallowReactive', 'Ref', 'ShallowRef', 'EmptyRef', 'EmptyShallowRef',
                    'NuxtError', 'Set', 'Map', 'Date'}

        def hydrate(idx, depth=0):
            if not isinstance(idx, int) or idx < 0 or idx >= len(data) or depth > 200:
                return None
            if idx in cache:
                return cache[idx]
            v = data[idx]
            if isinstance(v, list):
                if v and isinstance(v[0], str) and v[0] in wrappers:
                    res = hydrate(v[1], depth + 1) if len(v) > 1 else None
                else:
                    res = []
                    cache[idx] = res
                    res.extend(hydrate(x, depth + 1) for x in v)
            elif isinstance(v, dict):
                res = {}
                cache[idx] = res
                for kk, vv in v.items():
                    res[kk] = hydrate(vv, depth + 1)
            else:
                res = v
            cache[idx] = res
            return res

        return hydrate(0)

    def walk(self, obj, depth=0):
        """Recorre recursivamente dicts y listas."""
        if depth > 60:
            return
        if isinstance(obj, dict):
            yield obj
            for v in obj.values():
                yield from self.walk(v, depth + 1)
        elif isinstance(obj, list):
            for v in obj:
                yield from self.walk(v, depth + 1)

    # ------------------------------------------------------------------ #
    # Buscar el objeto "producto" dentro del JSON
    # ------------------------------------------------------------------ #
    def find_product_object(self, blobs, soup, url):
        ld_product = None
        best, best_score = None, 0

        # Palabras clave de la URL / og:title para reconocer al producto correcto
        slug = urllib.parse.urlparse(url).path.rstrip('/').split('/')[-1].lower()
        slug_tokens = [t for t in re.split(r'[-_]', slug) if len(t) > 2]
        og_title = self.meta(soup, 'og:title') or ''
        code = og_title.split()[0].lower() if og_title else ''

        for origin, blob in blobs:
            for d in self.walk(blob):
                # JSON-LD Product
                t = d.get('@type')
                if t == 'Product' or (isinstance(t, list) and 'Product' in t):
                    ld_product = ld_product or d

                name = self.first_str(d, self.NAME_KEYS)
                price = self.price_from_dict(d)
                if not name or price is None:
                    continue
                score = 1
                low = name.lower()
                score += sum(1 for tk in slug_tokens if tk in low)
                if code and code in low:
                    score += 3
                if any(isinstance(v, list) and v and isinstance(v[0], dict) for v in d.values()):
                    score += 1  # probablemente tenga variantes / imágenes
                if self.find_variant_list(d):
                    score += 3
                if score > best_score:
                    best, best_score = d, score

        if best:
            print(f"🎯 Objeto producto encontrado (score {best_score}): claves {list(best.keys())[:25]}")
        return best, ld_product

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def meta(self, soup, prop):
        tag = soup.find('meta', attrs={'property': prop}) or soup.find('meta', attrs={'name': prop})
        return tag.get('content', '').strip() if tag and tag.get('content') else None

    def first_str(self, d, keys):
        lower = {str(k).lower(): v for k, v in d.items()}
        for k in keys:
            v = lower.get(k)
            if isinstance(v, str) and v.strip() and not v.strip().startswith('$'):
                return v.strip()
        return None

    def parse_price(self, value):
        """Convierte '18.260,00', '18260.00', '$ 18.260', 18260 → 18260.0"""
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        s = re.sub(r'[^\d.,]', '', str(value))
        if not s or not re.search(r'\d', s):
            return None
        if ',' in s and '.' in s:
            if s.rfind(',') > s.rfind('.'):
                s = s.replace('.', '').replace(',', '.')   # 18.260,00
            else:
                s = s.replace(',', '')                     # 18,260.00
        elif ',' in s:
            s = s.replace(',', '.') if len(s.split(',')[-1]) == 2 else s.replace(',', '')
        elif s.count('.') > 1 or (s.count('.') == 1 and len(s.split('.')[-1]) == 3):
            s = s.replace('.', '')                         # 18.260
        try:
            return float(s)
        except ValueError:
            return None

    def price_from_dict(self, d):
        lower = {str(k).lower(): v for k, v in d.items()}
        for k in self.PRICE_KEYS_PREFERRED:
            if k in lower:
                p = self.parse_price(lower[k])
                if p and p > 0:
                    return p
        for k, v in lower.items():
            if ('precio' in k or 'price' in k) and not any(x in k for x in self.PRICE_KEYS_EXCLUDED):
                p = self.parse_price(v) if not isinstance(v, (dict, list)) else None
                if p and p > 0:
                    return p
        return None

    def attr_value(self, v):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return str(v)
        if isinstance(v, str):
            v = v.strip()
            return v if v and not v.startswith('#') and not v.startswith('http') else None
        if isinstance(v, dict):
            for k in ('nombre', 'name', 'descripcion', 'description', 'valor', 'value', 'label', 'titulo', 'title'):
                if isinstance(v.get(k), (str, int, float)) and str(v.get(k)).strip():
                    return str(v[k]).strip()
        return None

    def variant_attrs(self, d):
        """Devuelve (talle, color) de un dict de variante, si los tiene."""
        size = color = None
        lower = {str(k).lower(): v for k, v in d.items()}

        # claves exactas
        for k in self.SIZE_KEYS:
            if k in lower and size is None and not isinstance(lower[k], list):
                size = self.attr_value(lower[k])
        for k in self.COLOR_KEYS:
            if k in lower and color is None and not isinstance(lower[k], list):
                color = self.attr_value(lower[k])

        # claves que empiezan igual (ej. "talle_nombre", "colorNombre")
        for k, v in lower.items():
            if isinstance(v, list):
                continue
            if size is None and any(k.startswith(s) for s in self.SIZE_KEYS) and 'id' not in k:
                size = self.attr_value(v)
            if color is None and any(k.startswith(c) for c in self.COLOR_KEYS) and 'id' not in k \
                    and 'hex' not in k and 'code' not in k and 'codigo' not in k:
                color = self.attr_value(v)

        # listas de atributos: [{"nombre": "Talle", "valor": "M"}, ...]
        for v in lower.values():
            if isinstance(v, list):
                for a in v:
                    if isinstance(a, dict):
                        n = str(a.get('nombre') or a.get('name') or a.get('atributo') or a.get('attribute') or '').lower()
                        val = a.get('valor') or a.get('value') or a.get('opcion') or a.get('option')
                        val = self.attr_value(val)
                        if not val:
                            continue
                        if size is None and any(s in n for s in ('talle', 'size', 'talla', 'medida')):
                            size = val
                        elif color is None and any(c in n for c in ('color', 'colour')):
                            color = val
            elif isinstance(v, dict):  # {"Talle": "M", "Color": "Negro"}
                for kk, vv in v.items():
                    kl = str(kk).lower()
                    if size is None and any(s in kl for s in ('talle', 'size', 'talla')):
                        size = self.attr_value(vv)
                    elif color is None and 'color' in kl:
                        color = self.attr_value(vv)
        return size, color

    def is_available(self, d):
        lower = {str(k).lower(): v for k, v in d.items()}
        for k in ('agotado', 'sin_stock', 'sinstock', 'outofstock', 'out_of_stock'):
            if k in lower and isinstance(lower[k], bool):
                return not lower[k]
        for k in self.STOCK_KEYS:
            if k in lower:
                v = lower[k]
                # stock como objeto: {"total": 3}, {"disponible": 0}, {"quantity": 2}...
                if isinstance(v, dict):
                    sub = {str(kk).lower(): vv for kk, vv in v.items()}
                    for kk in ('total', 'disponible', 'available', 'cantidad', 'quantity', 'qty', 'stock'):
                        if kk in sub and not isinstance(sub[kk], (dict, list)):
                            v = sub[kk]
                            break
                    else:
                        continue
                if isinstance(v, bool):
                    return v
                if isinstance(v, (int, float)):
                    return v > 0
                if isinstance(v, str):
                    p = self.parse_price(v)
                    if p is not None:
                        return p > 0
                    return v.strip().lower() in ('si', 'sí', 'true', 'yes', 'disponible', 'instock')
        return True

    def key_kind(self, key):
        kl = str(key).lower()
        if kl in self.SIZE_KEYS:
            return 'size'
        if kl in self.COLOR_KEYS:
            return 'color'
        return None

    def item_attrs(self, item, kind=None):
        """Talle/color de un item; si la lista se llama 'colores' o 'talles', usa su nombre."""
        s, c = self.variant_attrs(item)
        if kind == 'color' and not c:
            c = self.attr_value(item)
        elif kind == 'size' and not s:
            s = self.attr_value(item)
        return s, c

    def find_variant_list(self, obj):
        """Busca la lista de dicts que mejor parezca una lista de variantes (talle/color).
        Devuelve (lista, tipo) donde tipo es 'size'/'color' si la clave padre lo indica."""
        best, best_kind, best_n = None, None, 0
        for d in self.walk(obj):
            for k, v in d.items():
                if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                    kind = self.key_kind(k)
                    n = sum(1 for x in v if any(self.item_attrs(x, kind)))
                    if n > best_n:
                        best, best_kind, best_n = v, kind, n
        return (best, best_kind) if best else None

    # ------------------------------------------------------------------ #
    # Extractores
    # ------------------------------------------------------------------ #
    def extract_name(self, soup, product_obj=None, ld_product=None):
        if product_obj:
            name = self.first_str(product_obj, self.NAME_KEYS)
            if name:
                return name
        if ld_product and isinstance(ld_product.get('name'), str):
            return ld_product['name'].strip()

        og = self.meta(soup, 'og:title') or self.meta(soup, 'twitter:title')
        if og:
            return og.split(' | ')[0].strip()

        # sitio viejo
        desc_input = soup.find('input', {'name': 'descripcion'})
        if desc_input and desc_input.get('value', '').strip():
            return desc_input['value'].strip()

        for selector in ('h1', 'h3', '.product-title', '.product-name'):
            el = soup.select_one(selector)
            if el and el.get_text(strip=True):
                return el.get_text(strip=True)

        if soup.title and soup.title.string:
            return soup.title.string.split(' | ')[0].strip()
        return "Producto Paulina Mayorista"

    def extract_price(self, soup, product_obj=None, ld_product=None, blobs=None):
        # 1) objeto producto del JSON embebido
        if product_obj:
            p = self.price_from_dict(product_obj)
            if p:
                print(f"✅ Precio desde JSON embebido: {p}")
                return p

        # 2) JSON-LD offers
        if ld_product:
            offers = ld_product.get('offers')
            for off in (offers if isinstance(offers, list) else [offers]):
                if isinstance(off, dict):
                    p = self.parse_price(off.get('price') or off.get('lowPrice'))
                    if p:
                        print(f"✅ Precio desde JSON-LD: {p}")
                        return p

        # 3) metaetiquetas
        for prop in ('product:price:amount', 'og:price:amount'):
            p = self.parse_price(self.meta(soup, prop))
            if p:
                return p
        el = soup.find(attrs={'itemprop': 'price'})
        if el:
            p = self.parse_price(el.get('content') or el.get_text())
            if p:
                return p

        # 4) sitio viejo: input hidden
        price_input = soup.find('input', {'name': 'precio'})
        if price_input and price_input.get('value'):
            p = self.parse_price(price_input['value'])
            if p:
                return p

        # 5) texto visible "$ 18.260,00"
        for selector in ('[class*="price"]', '[class*="precio"]', '.title strong', 'p.title strong'):
            for el in soup.select(selector):
                m = re.search(r'\$\s*([\d.,]+)', el.get_text(' ', strip=True))
                if m:
                    p = self.parse_price(m.group(1))
                    if p:
                        return p
        body = soup.body.get_text(' ', strip=True) if soup.body else ''
        m = re.search(r'\$\s*([\d]{1,3}(?:\.\d{3})+(?:,\d{2})?|\d{3,}(?:[.,]\d{2})?)', body)
        if m:
            p = self.parse_price(m.group(1))
            if p:
                print(f"⚠️ Precio desde texto visible: {p}")
                return p

        print("❌ No se encontró el precio")
        return 0.0

    IMG_RE = re.compile(r'(?:https?:)?//[^\s"\'<>\\]+?\.(?:jpe?g|png|webp)(?:\?[^\s"\'<>\\]*)?', re.I)

    def image_urls_in_html(self, html):
        """Todas las URLs de imágenes de producto del CDN que aparecen en el HTML/JSON."""
        text = html.replace('\\/', '/').replace('\\u002F', '/')
        urls = []
        for m in self.IMG_RE.finditer(text):
            u = m.group(0)
            if '/productos/' in u or 'r2.dev' in u or 'uploads/products' in u:
                if 'logo' not in u.lower() and u not in urls:
                    urls.append(u)
        return urls

    def extract_image(self, soup, base_url, product_obj=None, ld_product=None,
                      html='', blobs=None, code=None):
        print("🖼️ Buscando imagen PRINCIPAL del producto...")
        code_l = (code or '').lower()

        # 1) og:image (el sitio nuevo la publica siempre)
        og = self.meta(soup, 'og:image') or self.meta(soup, 'twitter:image')
        if og and 'logo' not in og.lower():
            return self.make_absolute_url(og, base_url)

        # 2) JSON-LD
        if ld_product:
            img = ld_product.get('image')
            if isinstance(img, list) and img:
                img = img[0]
            if isinstance(img, dict):
                img = img.get('url')
            if isinstance(img, str) and img:
                return self.make_absolute_url(img, base_url)

        # 2b) URLs del CDN cuyo nombre de archivo contiene el código (ej. ...-an27019.jpg)
        if code_l:
            for u in self.image_urls_in_html(html):
                if code_l in u.lower().rsplit('/', 1)[-1]:
                    print(f"✅ Imagen encontrada por código en el CDN: {u}")
                    return self.make_absolute_url(u, base_url)

        # 2c) la imagen más cercana al producto dentro del JSON embebido
        for d in self.related_dicts(blobs, code):
            for dd in self.walk(d):
                for k, v in dd.items():
                    if isinstance(v, str) and self.IMG_RE.fullmatch(v.strip()) and 'logo' not in v.lower():
                        print(f"✅ Imagen encontrada en JSON del producto ({k}): {v}")
                        return self.make_absolute_url(v.strip(), base_url)

        # 3) objeto producto: primera URL de imagen
        if product_obj:
            for d in self.walk(product_obj):
                for v in d.values():
                    if isinstance(v, str) and re.search(r'\.(jpe?g|png|webp)(\?|$)', v, re.I):
                        return self.make_absolute_url(v, base_url)

        # 4) <img> del CDN de productos
        for img in soup.find_all('img'):
            src = img.get('src') or img.get('data-src') or ''
            if any(p in src for p in ('/productos/', 'uploads/products/', 'r2.dev')):
                return self.make_absolute_url(src, base_url)

        # 5) sitio viejo
        for selector in ('.tz-gallery img', 'a.lightbox img'):
            el = soup.select_one(selector)
            if el and el.get('src'):
                return self.make_absolute_url(el['src'], base_url)

        print("❌ No se pudo encontrar la imagen del producto")
        return None

    def product_code(self, soup, url):
        """Código del artículo (ej. 'KA634M'), del og:title o del slug de la URL."""
        og = self.meta(soup, 'og:title') or ''
        first = og.split()[0] if og.split() else ''
        if re.search(r'\d', first):
            return first
        slug = urllib.parse.urlparse(url).path.rstrip('/').split('/')[-1]
        first = slug.split('-')[0]
        return first if re.search(r'\d', first) else None

    def related_dicts(self, blobs, code):
        """Dicts de todo el JSON embebido que mencionan el código del producto."""
        out = []
        if not code or not blobs:
            return out
        code_l = code.lower()
        for _, blob in blobs:
            for d in self.walk(blob):
                try:
                    dump = json.dumps(d, ensure_ascii=False).lower()
                except Exception:
                    continue
                if code_l in dump and len(dump) < 400_000:
                    out.append(d)
        # de más chico a más grande: el más chico que contenga variantes es el más específico
        out.sort(key=lambda d: len(json.dumps(d, ensure_ascii=False)))
        return out

    def extract_sizes_and_colors(self, soup, product_obj=None, blobs=None, code=None):
        print("🎨 Extrayendo talles y colores...")
        data = {'sizes': [], 'colors': [], 'availability': {}}

        # --- 1) JSON embebido: lista de variantes ---
        sources = ([product_obj] if product_obj else []) + self.related_dicts(blobs, code)
        combos = []  # (talle, color, disponible)
        for src in sources:
            found = self.find_variant_list(src)
            if not found:
                continue
            vlist, kind = found
            for item in vlist:
                s, c = self.item_attrs(item, kind)
                avail = self.is_available(item)
                nested = None
                # variante "color" que contiene sus talles (o al revés)
                for nk, v in item.items():
                    if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                        sub = [(self.item_attrs(x, self.key_kind(nk)), self.is_available(x)) for x in v]
                        if any(a[0][0] or a[0][1] for a in sub):
                            nested = sub
                            break
                if nested and (bool(s) != bool(c)):
                    for (ns, nc), navail in nested:
                        combos.append((s or ns, c or nc, navail))
                elif s or c:
                    combos.append((s, c, avail))
            if combos:
                break

        # --- 2) JSON embebido: listas sueltas "talles": [...], "colores": [...] ---
        for src in (sources if not combos else []):
            sizes, colors = [], []
            for d in self.walk(src):
                for k, v in d.items():
                    kl = str(k).lower()
                    if isinstance(v, list) and v:
                        vals = [self.attr_value(x) for x in v]
                        vals = [x for x in vals if x]
                        if kl in self.SIZE_KEYS and vals and not sizes:
                            sizes = vals
                        elif kl in self.COLOR_KEYS and vals and not colors:
                            colors = vals
            for sz in sizes or [None]:
                for c in colors or [None]:
                    if sz or c:
                        combos.append((sz, c, True))
            if combos:
                break

        if combos:
            for s, c, _ in combos:
                s = s or 'UNICO'
                c = c or 'ÚNICO'
                if s not in data['sizes']:
                    data['sizes'].append(s)
                if c not in data['colors']:
                    data['colors'].append(c)
            for c in data['colors']:
                data['availability'][c] = {s: False for s in data['sizes']}
            for s, c, avail in combos:
                s = s or 'UNICO'
                c = c or 'ÚNICO'
                data['availability'][c][s] = data['availability'][c][s] or bool(avail)
            # quitar colores sin ningún talle disponible
            data['colors'] = [c for c in data['colors'] if any(data['availability'][c].values())]
            data['availability'] = {c: data['availability'][c] for c in data['colors']}
            print(f"✅ Talles: {data['sizes']} | Colores: {data['colors']}")
            return data

        # --- 3) Sitio viejo: tabla HTML ---
        try:
            table = soup.find('table')
            if table:
                thead = table.find('thead')
                if thead:
                    for th in thead.find_all('th')[1:]:
                        t = th.get_text(strip=True)
                        if t:
                            data['sizes'].append(t)
                if not data['sizes']:
                    data['sizes'] = ['UNICO']
                tbody = table.find('tbody')
                if tbody:
                    for span in tbody.find_all('span'):
                        c = span.get_text(strip=True)
                        if c and c not in data['colors']:
                            data['colors'].append(c)
                            data['availability'][c] = {s: True for s in data['sizes']}
        except Exception as e:
            print(f"❌ Error leyendo tabla: {e}")

        print(f"✅ Talles: {data['sizes']} | Colores: {data['colors']}")
        return data

    # ------------------------------------------------------------------ #
    # Diagnóstico (para el botón "Probar Scraping")
    # ------------------------------------------------------------------ #
    def build_diagnostics(self, html, soup, blobs, product_obj, code=None):
        scripts = soup.find_all('script')
        text = soup.body.get_text(' ', strip=True) if soup.body else ''
        precio_ctx = [text[max(0, m.start() - 60): m.end() + 60]
                      for m in re.finditer(r'\$\s*[\d.,]{3,}', text)][:5]
        api_urls = sorted(set(re.findall(r'https?://[^\s"\'<>\\]*api[^\s"\'<>\\]*', html)))[:20]
        return {
            'html_bytes': len(html),
            'scripts_totales': len(scripts),
            'scripts_externos': [s.get('src') for s in scripts if s.get('src')][:15],
            'tiene___NEXT_DATA__': bool(soup.find('script', id='__NEXT_DATA__')),
            'tiene_rsc_next_f': '__next_f' in html,
            'tiene___NUXT_DATA__': bool(soup.find('script', id='__NUXT_DATA__')),
            'bloques_json': len(blobs),
            'origenes_json': sorted(set(o for o, _ in blobs)),
            'objeto_producto_claves': list(product_obj.keys())[:40] if product_obj else None,
            'objeto_producto_muestra': json.dumps(product_obj, ensure_ascii=False)[:3000] if product_obj else None,
            'textos_con_precio': precio_ctx,
            'urls_api_en_html': api_urls,
            'texto_visible_inicio': text[:800],
            'codigo_producto': code,
            'og_image': self.meta(soup, 'og:image'),
            'imagenes_cdn_en_html': self.image_urls_in_html(html)[:15],
            'dicts_que_mencionan_codigo': len(self.related_dicts(blobs, code)),
            'fragmentos_talle_color_en_html': [
                html[max(0, m.start() - 150): m.end() + 250].replace('\\"', '"')
                for m in list(re.finditer(r'(?i)talle|"colou?r', html))[:8]
            ],
        }

    def make_absolute_url(self, img_src, base_url):
        img_src = img_src.strip()
        if img_src.startswith('//'):
            return 'https:' + img_src
        if img_src.startswith('http'):
            return img_src
        parsed = urllib.parse.urlparse(base_url)
        return f"{parsed.scheme}://{parsed.netloc}/{img_src.lstrip('/')}"


class ImageGenerator:
    """
    Arma la imagen final en un lienzo de ancho fijo (1080 px, ideal para redes/WhatsApp).
    El ALTO se calcula a partir de lo que hay que dibujar, así nada queda afuera:
        [tabla talles/colores] + [foto del producto] + [título] + [precio]
    """
    CANVAS_W = 1080
    MARGIN = 40
    GAP = 30
    MAX_PRODUCT_H = 1250

    FONT_CANDIDATES = [
        "arial.ttf", "Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "DejaVuSans.ttf",
    ]
    BOLD_FONT_CANDIDATES = [
        "arialbd.ttf", "Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "DejaVuSans-Bold.ttf",
    ]

    # ------------------------------------------------------------------ #
    def generate_product_image(self, product_data, price_formula="x * 1.55"):
        try:
            print(f"🎨 Generando imagen para: {product_data['name']}")
            W, M, GAP = self.CANVAS_W, self.MARGIN, self.GAP
            content_w = W - 2 * M

            # Medidor para calcular tamaños de texto antes de dibujar
            measure = ImageDraw.Draw(Image.new('RGB', (10, 10)))

            # 1) Tabla de talles y colores
            table = self.prepare_table(measure, product_data.get('sizes_colors') or {}, content_w)
            table_h = table['height'] if table else 0

            # 2) Foto del producto (escalada al ancho útil)
            product_image = self.get_product_image(product_data['image_url'])
            pw, ph = product_image.size
            ratio = min(content_w / pw, self.MAX_PRODUCT_H / ph)
            new_size = (max(1, int(pw * ratio)), max(1, int(ph * ratio)))
            product_image = product_image.resize(new_size, Image.Resampling.LANCZOS)

            # 3) Título (ajustado por ancho real en píxeles, máx. 3 líneas)
            name = ' '.join(str(product_data['name']).split())
            title_size = 52 if len(name) <= 30 else 46 if len(name) <= 50 else 40
            title_font = self.get_font(title_size)
            title_lines = self.wrap_text(measure, name, title_font, content_w, max_lines=3)
            title_line_h = self.text_height(measure, "ÁgjpqyÑ", title_font) + 12
            title_h = title_line_h * len(title_lines)

            # 4) Precio
            modified_price = self.calculate_price(product_data['price'], price_formula)
            print(f"💰 Precio original: {product_data['price']}, Precio modificado: {modified_price}")
            price_text = f"${modified_price:.2f}"
            price_font = self.fit_font(measure, price_text, 110, content_w, bold=True)
            price_h = self.text_height(measure, price_text, price_font)

            # 5) Alto total del lienzo
            H = M + (table_h + GAP if table_h else 0) + new_size[1] + GAP + title_h + GAP + price_h + M
            final_image = Image.new('RGB', (W, H), 'white')
            draw = ImageDraw.Draw(final_image)

            y = M
            if table:
                self.draw_table(draw, table, (W - table['width']) // 2, y)
                y += table_h + GAP

            final_image.paste(product_image, ((W - new_size[0]) // 2, y))
            y += new_size[1] + GAP

            for line in title_lines:
                draw.text((W // 2, y), line, fill='black', font=title_font, anchor="ma")
                y += title_line_h
            y += GAP - 12

            draw.text((W // 2, y), price_text, fill='#d0021b', font=price_font, anchor="ma",
                      stroke_width=2, stroke_fill='#d0021b')

            print(f"📐 Imagen final: {W}x{H}")
            return final_image

        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"❌ Error generando imagen: {e}")
            return None

    # ------------------------------------------------------------------ #
    # Fuentes
    # ------------------------------------------------------------------ #
    def get_font(self, size, bold=False):
        cands = (self.BOLD_FONT_CANDIDATES if bold else []) + self.FONT_CANDIDATES
        for fp in cands:
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                continue
        try:
            return ImageFont.load_default(size=size)   # Pillow >= 10.1 (fuente escalable incluida)
        except TypeError:
            return ImageFont.load_default()

    def text_width(self, draw, text, font):
        return draw.textlength(text, font=font)

    def text_height(self, draw, text, font):
        box = draw.textbbox((0, 0), text, font=font)
        return box[3] - box[1]

    def fit_font(self, draw, text, size, max_w, bold=False, min_size=14):
        font = self.get_font(size, bold)
        while size > min_size and self.text_width(draw, text, font) > max_w:
            size -= 2
            font = self.get_font(size, bold)
        return font

    def wrap_text(self, draw, text, font, max_w, max_lines=3):
        words, lines, current = text.split(), [], ''
        for i, word in enumerate(words):
            test = f"{current} {word}".strip()
            if self.text_width(draw, test, font) <= max_w:
                current = test
                continue
            if current:
                lines.append(current)
            current = word
            if len(lines) == max_lines - 1:
                current = ' '.join(words[i:])
                break
        if current:
            if self.text_width(draw, current, font) > max_w:
                while current and self.text_width(draw, current + '…', font) > max_w:
                    current = current[:-1]
                current = current.rstrip() + '…'
            lines.append(current)
        return lines or [text]

    # ------------------------------------------------------------------ #
    # Tabla de talles y colores
    # ------------------------------------------------------------------ #
    def prepare_table(self, draw, data, max_w):
        sizes = data.get('sizes') or []
        colors = data.get('colors') or []
        if not sizes or not colors:
            print("ℹ️ No hay datos de talles/colores para mostrar")
            return None

        for font_size in range(30, 13, -2):
            head_font = self.get_font(font_size, bold=True)
            cell_font = self.get_font(font_size)
            pad = font_size
            color_col = max(self.text_width(draw, "COLORES", head_font),
                            *(self.text_width(draw, c, cell_font) for c in colors)) + 2 * pad
            size_col = max(font_size * 2.6,
                           *(self.text_width(draw, str(s), head_font) + 2 * pad for s in sizes))
            width = int(color_col + size_col * len(sizes))
            if width <= max_w:
                break
        # si aún así no entra, se achica el ancho de la columna de colores
        if width > max_w:
            color_col = max(120, max_w - size_col * len(sizes))
            width = int(color_col + size_col * len(sizes))

        row_h = int(font_size * 1.9)
        return {
            'sizes': sizes, 'colors': colors, 'availability': data.get('availability', {}),
            'head_font': head_font, 'cell_font': cell_font,
            'color_col': int(color_col), 'size_col': int(size_col), 'row_h': row_h,
            'width': width, 'height': row_h * (len(colors) + 1), 'font_size': font_size,
        }

    def draw_table(self, draw, t, left, top):
        cc, sc, rh = t['color_col'], t['size_col'], t['row_h']
        border = '#dee2e6'

        # Encabezado
        draw.rectangle([left, top, left + cc, top + rh], fill='#343a40', outline=border)
        draw.text((left + cc / 2, top + rh / 2), "COLORES", fill='white', font=t['head_font'], anchor="mm")
        for i, s in enumerate(t['sizes']):
            x = left + cc + i * sc
            draw.rectangle([x, top, x + sc, top + rh], fill='#343a40', outline=border)
            draw.text((x + sc / 2, top + rh / 2), str(s), fill='white', font=t['head_font'], anchor="mm")

        # Filas
        mark = max(6, int(t['font_size'] * 0.35))
        lw = max(2, t['font_size'] // 9)
        for r, color in enumerate(t['colors']):
            y = top + (r + 1) * rh
            draw.rectangle([left, y, left + cc, y + rh], fill='#e9ecef', outline=border)
            label = color
            while label and self.text_width(draw, label, t['cell_font']) > cc - t['font_size']:
                label = label[:-1]
            if label != color:
                label = label[:-1] + '…'
            draw.text((left + t['font_size'] / 2, y + rh / 2), label, fill='black',
                      font=t['cell_font'], anchor="lm")

            for i, s in enumerate(t['sizes']):
                x = left + cc + i * sc
                ok = t['availability'].get(color, {}).get(s, False)
                draw.rectangle([x, y, x + sc, y + rh], fill='#d4edda' if ok else '#f8d7da', outline=border)
                cx, cy = x + sc / 2, y + rh / 2
                col = '#155724' if ok else '#721c24'
                if ok:
                    draw.line([(cx - mark, cy), (cx - mark / 3, cy + mark * 0.8), (cx + mark, cy - mark)],
                              fill=col, width=lw, joint='curve')
                else:
                    draw.line([(cx - mark * 0.8, cy - mark * 0.8), (cx + mark * 0.8, cy + mark * 0.8)], fill=col, width=lw)
                    draw.line([(cx - mark * 0.8, cy + mark * 0.8), (cx + mark * 0.8, cy - mark * 0.8)], fill=col, width=lw)

    # ------------------------------------------------------------------ #
    def get_product_image(self, image_url):
        """Obtener imagen del producto"""
        if image_url:
            try:
                print(f"📥 Descargando imagen: {image_url}")
                response = scraper.session.get(image_url, timeout=20)
                response.raise_for_status()

                # Verificar que sea una imagen
                content_type = response.headers.get('content-type', '')
                if 'image' not in content_type:
                    print(f"❌ URL no es una imagen: {content_type}")
                    return self.create_placeholder()

                image = Image.open(io.BytesIO(response.content))
                if image.mode in ('RGBA', 'LA', 'P'):
                    image = image.convert('RGBA')
                    fondo = Image.new('RGB', image.size, 'white')
                    fondo.paste(image, mask=image.split()[-1])
                    image = fondo
                else:
                    image = image.convert('RGB')
                print(f"✅ Imagen descargada correctamente: {image.size}")
                return image

            except Exception as e:
                print(f"❌ Error descargando imagen: {e}")

        return self.create_placeholder()

    def create_placeholder(self):
        """Crear imagen placeholder de tamaño estándar"""
        placeholder = Image.new('RGB', (400, 400), color='lightgray')
        draw = ImageDraw.Draw(placeholder)

        try:
            font = ImageFont.truetype("arial.ttf", 20)
        except:
            font = ImageFont.load_default()

        draw.text((200, 200), "Imagen no disponible", fill='darkgray', font=font, anchor="mm")
        return placeholder

    def calculate_price(self, original_price, formula):
        """Calcular precio con fórmula y redondeo inteligente"""
        try:
            expression = formula.replace('x', str(original_price))
            result = eval(expression)
            print(f"🧮 Fórmula aplicada: {formula} = {result}")

            # Aplicar redondeo inteligente basado en el precio
            result = self.smart_round_price(result, formula)

            return result

        except Exception as e:
            print(f"❌ Error en fórmula, usando valor por defecto: {e}")
            return self.smart_round_price(original_price * 1.55, "x * 1.55")

    def smart_round_price(self, price, formula):
        """
        Redondeo inteligente basado en el precio y la fórmula
        """
        print(f"💰 Precio antes de redondeo: {price}")

        # Detectar si es un recargo del 55%
        is_55_percent = any(trigger in formula for trigger in ['1.55', '0.55', '55%'])

        if is_55_percent:
            # Para recargo del 55%, usar múltiplo de 500
            multiple = 500
            rounded_price = self.round_to_nearest(price, multiple, round_up=True)
            print(f"🎯 Recargo 55% detectado - Redondeando a múltiplo de {multiple}: {rounded_price}")

        elif price > 50000:
            # Precios altos: múltiplo de 1000
            multiple = 1000
            rounded_price = self.round_to_nearest(price, multiple, round_up=True)
            print(f"📈 Precio alto - Redondeando a múltiplo de {multiple}: {rounded_price}")

        elif price > 10000:
            # Precios medios: múltiplo de 500
            multiple = 500
            rounded_price = self.round_to_nearest(price, multiple, round_up=True)
            print(f"⚖️ Precio medio - Redondeando a múltiplo de {multiple}: {rounded_price}")

        else:
            # Precios bajos: múltiplo de 100
            multiple = 100
            rounded_price = self.round_to_nearest(price, multiple, round_up=True)
            print(f"📉 Precio bajo - Redondeando a múltiplo de {multiple}: {rounded_price}")

        return rounded_price

    def round_to_nearest(self, number, multiple=500, round_up=True):
        """
        Redondear un número al múltiplo más cercano
        """
        if multiple == 0:
            return number

        if round_up:
            # Redondear siempre hacia arriba
            rounded = math.ceil(number / multiple) * multiple
        else:
            # Redondear al múltiplo más cercano
            rounded = round(number / multiple) * multiple

        print(f"🔢 Redondeo: {number:.2f} → {rounded:.2f} (múltiplo de {multiple})")
        return rounded



# Instancias globales
scraper = PaulinaScraper()
image_gen = ImageGenerator()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/debug-scrape', methods=['POST'])
def debug_scrape():
    data = request.json
    url = data.get('url')

    if not url:
        return jsonify({'success': False, 'error': 'URL requerida'})

    print(f"🐛 Debug scraping para: {url}")
    product_data = scraper.scrape_product(url, debug=True)
    return jsonify({'success': True, 'debug_data': product_data})


@app.route('/generate-image', methods=['POST'])
def generate_image():
    data = request.json
    url = data.get('url')
    formula = data.get('formula', 'x * 1.55')

    if not url:
        return jsonify({'success': False, 'error': 'URL requerida'})

    print(f"🚀 Generando imagen para: {url}")
    print(f"🧮 Usando fórmula: {formula}")

    product_data = scraper.scrape_product(url)

    if 'error' in product_data:
        return jsonify({'success': False, 'error': product_data['error']})

    # Generar imagen (sin guardar)
    final_image = image_gen.generate_product_image(product_data, formula)

    if final_image:
        # Crear un ID único para la imagen
        image_id = hashlib.md5(f"{url}{formula}".encode()).hexdigest()[:10]

        return jsonify({
            'success': True,
            'image_url': f'/download/{image_id}?url={urllib.parse.quote(url)}&formula={urllib.parse.quote(formula)}',
            'product_data': product_data,
            'calculated_price': image_gen.calculate_price(product_data['price'], formula)
        })
    else:
        return jsonify({'success': False, 'error': 'Error generando imagen'})


@app.route('/download/<image_id>')
def download_file(image_id):
    try:
        # Obtener parámetros de la URL
        product_url = request.args.get('url')
        formula = request.args.get('formula', 'x * 1.55')

        if not product_url:
            return jsonify({'success': False, 'error': 'URL no proporcionada'})

        print(f"📥 Generando imagen para descarga: {product_url}")

        # Obtener datos del producto
        product_data = scraper.scrape_product(product_url)

        if 'error' in product_data:
            return jsonify({'success': False, 'error': product_data['error']})

        # Generar imagen al vuelo
        final_image = image_gen.generate_product_image(product_data, formula)

        if not final_image:
            return jsonify({'success': False, 'error': 'Error generando imagen'})

        # Convertir a bytes en memoria
        img_io = io.BytesIO()
        final_image.save(img_io, 'JPEG', quality=95)
        img_io.seek(0)

        # Crear nombre de archivo para descarga
        safe_name = re.sub(r'[^\w\-_.]', '_', product_data['name'])
        filename = f"producto_{safe_name}.jpg"

        # Enviar imagen directamente sin guardar
        return send_file(
            img_io,
            mimetype='image/jpeg',
            as_attachment=True,
            download_name=filename
        )

    except Exception as e:
        print(f"❌ Error en descarga: {e}")
        return jsonify({'success': False, 'error': 'Error generando imagen para descarga'})


if __name__ == '__main__':
    # Configuración para producción
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "False").lower() == "true"

    print("🚀 Servidor iniciado - Modo sin almacenamiento temporal")
    print("💡 Las imágenes se generan al vuelo sin guardar archivos")

    app.run(
        host="0.0.0.0",
        port=port,
        debug=debug
    )