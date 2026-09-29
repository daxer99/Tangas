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
                'image_url': self.extract_image(soup, url, product_obj, ld_product),
                'sizes_colors': self.extract_sizes_and_colors(soup, product_obj, blobs),
                'original_url': url
            }

            if debug:
                product_data['diagnostico'] = self.build_diagnostics(html, soup, blobs, product_obj)

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

    def extract_image(self, soup, base_url, product_obj=None, ld_product=None):
        print("🖼️ Buscando imagen PRINCIPAL del producto...")

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

    def extract_sizes_and_colors(self, soup, product_obj=None, blobs=None):
        print("🎨 Extrayendo talles y colores...")
        data = {'sizes': [], 'colors': [], 'availability': {}}

        # --- 1) JSON embebido: lista de variantes ---
        sources = [product_obj] if product_obj else []
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
        if not combos and product_obj:
            sizes, colors = [], []
            for d in self.walk(product_obj):
                for k, v in d.items():
                    kl = str(k).lower()
                    if isinstance(v, list) and v:
                        vals = [self.attr_value(x) for x in v]
                        vals = [x for x in vals if x]
                        if kl in self.SIZE_KEYS and vals and not sizes:
                            sizes = vals
                        elif kl in self.COLOR_KEYS and vals and not colors:
                            colors = vals
            for s in sizes or [None]:
                for c in colors or [None]:
                    if s or c:
                        combos.append((s, c, True))

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
    def build_diagnostics(self, html, soup, blobs, product_obj):
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
    def generate_product_image(self, product_data, price_formula="x * 1.55"):
        try:
            print(f"🎨 Generando imagen para: {product_data['name']}")

            # Crear imagen del producto
            product_image = self.get_product_image(product_data['image_url'])

            # Obtener dimensiones de la imagen original
            original_width, original_height = product_image.size
            print(f"📐 Dimensiones originales: {original_width}x{original_height}")

            # Calcular dimensiones del canvas final (más alto para la tabla)
            canvas_width, canvas_height, product_size, product_position = self.calculate_layout(
                original_width, original_height, product_data.get('sizes_colors')
            )

            # Crear imagen final con dimensiones dinámicas
            final_image = Image.new('RGB', (canvas_width, canvas_height), color='white')
            draw = ImageDraw.Draw(final_image)

            # Dibujar tabla de talles y colores si existe
            table_height = self.draw_sizes_colors_table(
                draw, product_data.get('sizes_colors', {}),
                canvas_width, canvas_height
            )

            # Ajustar posición del producto para dejar espacio para la tabla
            adjusted_product_position = (product_position[0], product_position[1] + table_height)

            # Redimensionar y pegar imagen del producto manteniendo relación de aspecto
            resized_product = self.resize_product_image(product_image, product_size)
            final_image.paste(resized_product, adjusted_product_position)

            # Configurar fuentes
            title_font, price_font, table_font = self.load_fonts(canvas_width, product_data['name'])

            # Calcular precio
            original_price = product_data['price']
            modified_price = self.calculate_price(original_price, price_formula)

            print(f"💰 Precio original: {original_price}, Precio modificado: {modified_price}")

            # Dibujar textos en posiciones dinámicas
            self.draw_texts(draw, product_data['name'], modified_price, title_font, price_font,
                            canvas_width, canvas_height, adjusted_product_position, product_size)

            # Devolver imagen en memoria (sin guardar)
            return final_image

        except Exception as e:
            print(f"❌ Error generando imagen: {e}")
            return None

    def draw_sizes_colors_table(self, draw, sizes_colors_data, canvas_width, canvas_height):
        """Dibujar tabla de talles y colores en la parte superior"""
        if not sizes_colors_data or not sizes_colors_data.get('sizes') or not sizes_colors_data.get('colors'):
            print("ℹ️ No hay datos de talles/colores para mostrar")
            return 0

        try:
            sizes = sizes_colors_data['sizes']
            colors = sizes_colors_data['colors']
            availability = sizes_colors_data.get('availability', {})

            print(f"📊 Dibujando tabla: {len(colors)} colores x {len(sizes)} talles")

            # Configuración de la tabla
            table_top = 20
            row_height = 30
            col_width = 80
            color_col_width = 150

            # Calcular ancho total de la tabla
            table_width = color_col_width + (len(sizes) * col_width)

            # Centrar la tabla horizontalmente
            table_left = (canvas_width - table_width) // 2

            # Fuentes
            try:
                header_font = ImageFont.truetype("arial.ttf", 14)
                cell_font = ImageFont.truetype("arial.ttf", 12)
            except:
                header_font = ImageFont.load_default()
                cell_font = ImageFont.load_default()

            # Dibujar fondo de la tabla
            table_height = (len(colors) + 1) * row_height
            draw.rectangle([table_left, table_top, table_left + table_width, table_top + table_height],
                           fill='#f8f9fa', outline='#dee2e6')

            # Dibujar encabezados de talles
            for i, size in enumerate(sizes):
                x = table_left + color_col_width + (i * col_width)
                y = table_top

                # Celda del encabezado
                draw.rectangle([x, y, x + col_width, y + row_height], fill='#343a40', outline='#dee2e6')

                # Texto del talle
                draw.text((x + col_width / 2, y + row_height / 2), str(size),
                          fill='white', font=header_font, anchor="mm")

            # Dibujar encabezado de colores
            draw.rectangle([table_left, table_top, table_left + color_col_width, table_top + row_height],
                           fill='#343a40', outline='#dee2e6')
            draw.text((table_left + color_col_width / 2, table_top + row_height / 2), "COLORES",
                      fill='white', font=header_font, anchor="mm")

            # Dibujar filas de colores
            for row_idx, color in enumerate(colors):
                y = table_top + (row_idx + 1) * row_height

                # Celda del color
                draw.rectangle([table_left, y, table_left + color_col_width, y + row_height],
                               fill='#e9ecef', outline='#dee2e6')

                # Texto del color (truncar si es muy largo)
                color_display = color[:18] + "..." if len(color) > 18 else color
                draw.text((table_left + 5, y + row_height / 2), color_display,
                          fill='black', font=cell_font, anchor="lm")

                # Celdas de disponibilidad por talle
                for col_idx, size in enumerate(sizes):
                    x = table_left + color_col_width + (col_idx * col_width)

                    # Verificar disponibilidad
                    is_available = availability.get(color, {}).get(size, False)
                    cell_color = '#d4edda' if is_available else '#f8d7da'
                    text_color = '#155724' if is_available else '#721c24'
                    symbol = '✓' if is_available else '✗'

                    draw.rectangle([x, y, x + col_width, y + row_height],
                                   fill=cell_color, outline='#dee2e6')
                    # Dibujar ✓ / ✗ con líneas (no depende de que la fuente tenga esos símbolos)
                    cx, cy = x + col_width / 2, y + row_height / 2
                    if is_available:
                        draw.line([(cx - 7, cy), (cx - 2, cy + 6), (cx + 8, cy - 7)], fill=text_color, width=3)
                    else:
                        draw.line([(cx - 6, cy - 6), (cx + 6, cy + 6)], fill=text_color, width=3)
                        draw.line([(cx - 6, cy + 6), (cx + 6, cy - 6)], fill=text_color, width=3)

            print(f"✅ Tabla dibujada: {table_height}px de altura")
            return table_height + 10  # Altura total + margen

        except Exception as e:
            print(f"❌ Error dibujando tabla: {e}")
            return 0

    def calculate_layout(self, img_width, img_height, sizes_colors_data=None):
        """Calcular layout dinámico considerando la tabla"""
        # Altura base adicional para la tabla
        table_height = 0
        if sizes_colors_data and sizes_colors_data.get('sizes') and sizes_colors_data.get('colors'):
            num_rows = len(sizes_colors_data['colors']) + 1  # +1 para el encabezado
            table_height = num_rows * 35 + 50  # Estimación de altura

        # Determinar el tamaño del canvas
        if img_width > 800 or img_height > 600:
            canvas_width = max(800, img_width + 100)
            canvas_height = max(600 + table_height, img_height + 200 + table_height)
        elif img_width < 300 or img_height < 300:
            canvas_width = 800
            canvas_height = 600 + table_height
        else:
            canvas_width = img_width + 100
            canvas_height = img_height + 200 + table_height

        # Calcular tamaño y posición del producto
        if img_width > canvas_width - 100 or img_height > canvas_height - 200 - table_height:
            max_product_width = canvas_width - 100
            max_product_height = canvas_height - 200 - table_height

            ratio = min(max_product_width / img_width, max_product_height / img_height)
            product_width = int(img_width * ratio)
            product_height = int(img_height * ratio)
        else:
            product_width = min(img_width, canvas_width - 100)
            product_height = min(img_height, canvas_height - 200 - table_height)

        # Centrar la imagen horizontalmente
        x_position = (canvas_width - product_width) // 2
        y_position = 50  # Margen superior base (se ajustará con table_height)

        print(f"📏 Canvas: {canvas_width}x{canvas_height}, Producto: {product_width}x{product_height}")
        print(f"📍 Posición: ({x_position}, {y_position})")

        return canvas_width, canvas_height, (product_width, product_height), (x_position, y_position)

    def resize_product_image(self, image, target_size):
        """Redimensionar imagen manteniendo relación de aspecto"""
        width, height = target_size

        # Mantener relación de aspecto
        original_width, original_height = image.size
        ratio = min(width / original_width, height / original_height)

        new_width = int(original_width * ratio)
        new_height = int(original_height * ratio)

        return image.resize((new_width, new_height), Image.Resampling.LANCZOS)

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

    def load_fonts(self, canvas_width, product_name):
        """Cargar fuentes con título dinámico y precio fijo grande"""
        try:
            # TAMAÑO FIJO GRANDE para el precio (siempre igual)
            price_font_size = 52
            table_font_size = 14

            # TAMAÑO DINÁMICO para el título (se ajusta según longitud)
            name_length = len(product_name)

            if name_length > 60:
                title_font_size = 24  # Más pequeño para nombres muy largos
            elif name_length > 40:
                title_font_size = 28  # Mediano para nombres largos
            elif name_length > 25:
                title_font_size = 32  # Normal para nombres medianos
            else:
                title_font_size = 36  # Grande para nombres cortos

            # Intentar cargar fuentes
            font_loaded = False
            font_path = None

            # Lista de fuentes a probar
            font_paths = [
                "arial.ttf",
                "DejaVuSans.ttf",
                "LiberationSans-Regular.ttf"
            ]

            for fp in font_paths:
                try:
                    font = ImageFont.truetype(fp, title_font_size)
                    font_loaded = True
                    font_path = fp
                    print(f"✅ Fuente cargada: {font_path}")
                    break
                except:
                    continue

            if font_loaded:
                title_font = ImageFont.truetype(font_path, title_font_size)
                price_font = ImageFont.truetype(font_path, price_font_size)
                table_font = ImageFont.truetype(font_path, table_font_size)
            else:
                # Fuentes por defecto con ajustes de tamaño
                print("⚠️  Usando fuentes por defecto")
                title_font = ImageFont.load_default()
                price_font = ImageFont.load_default()
                table_font = ImageFont.load_default()
                # Ajustar tamaños para fuentes por defecto
                if name_length > 60:
                    title_font_size = 30
                elif name_length > 40:
                    title_font_size = 35
                elif name_length > 25:
                    title_font_size = 40
                else:
                    title_font_size = 45
                price_font_size = 65

            print(f"🎯 Tamaños - Título: {title_font_size}px ({name_length} chars), Precio: {price_font_size}px")

        except Exception as e:
            print(f"❌ Error cargando fuentes: {e}")
            title_font = ImageFont.load_default()
            price_font = ImageFont.load_default()
            table_font = ImageFont.load_default()

        return title_font, price_font, table_font

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

    def draw_texts(self, draw, name, price, title_font, price_font,
                   canvas_width, canvas_height, product_position, product_size):
        """Dibujar textos con mejor espaciado para múltiples líneas"""
        product_x, product_y = product_position
        product_width, product_height = product_size

        # Calcular posición Y para los textos
        text_start_y = product_y + product_height + 35

        # Dividir el nombre en líneas
        wrapped_lines = self.wrap_text(name, title_font, canvas_width - 100)

        # Dibujar nombre del producto
        if isinstance(wrapped_lines, list):
            # Texto multilínea
            line_height = 38  # Espacio entre líneas
            total_text_height = len(wrapped_lines) * line_height

            for i, line in enumerate(wrapped_lines):
                y_position = text_start_y + (i * line_height)
                draw.text((canvas_width // 2, y_position), line,
                          fill='black', font=title_font, anchor="mm")

            # Posición del precio
            price_y = text_start_y + total_text_height + 30
        else:
            # Texto de una línea
            draw.text((canvas_width // 2, text_start_y), wrapped_lines,
                      fill='black', font=title_font, anchor="mm")
            price_y = text_start_y + 65

        # Dibujar precio (SIEMPRE GRANDE)
        price_text = f"${price:.2f}"
        draw.text((canvas_width // 2, price_y), price_text,
                  fill='red', font=price_font, anchor="mm")

    def wrap_text(self, text, font, max_width):
        """Versión definitiva - Divide por palabras respetando límites"""
        # Limpiar texto de espacios extras
        text = ' '.join(text.split())

        # Si el texto es corto, devolver como está
        if len(text) <= 22:
            return text

        # Límite de caracteres por línea (ajustado para mayúsculas)
        base_chars_per_line = 22
        uppercase_count = sum(1 for c in text if c.isupper())
        total_chars = len(text)

        if uppercase_count / total_chars > 0.6:  # Muchas mayúsculas
            chars_per_line = 18
        elif uppercase_count / total_chars > 0.4:  # Bastantes mayúsculas
            chars_per_line = 20
        else:  # Texto normal
            chars_per_line = base_chars_per_line

        words = text.split()
        lines = []
        current_line = []
        current_length = 0

        for word in words:
            word_len = len(word)
            space_len = 1 if current_line else 0  # Espacio si no es primera palabra

            # Si agregar esta palabra excede el límite
            if current_length + word_len + space_len > chars_per_line:
                if current_line:
                    # Guardar línea actual
                    lines.append(' '.join(current_line))
                    current_line = []
                    current_length = 0

                # Si ya tenemos 2 líneas, manejar la tercera especial
                if len(lines) >= 2:
                    # Para la tercera línea, truncar lo que queda
                    remaining_words = ' '.join([word] + words[words.index(word) + 1:])
                    if len(remaining_words) > chars_per_line - 3:
                        # Buscar punto de corte natural
                        if ' ' in remaining_words[:chars_per_line - 3]:
                            cut_point = remaining_words[:chars_per_line - 3].rfind(' ')
                            if cut_point > 10:  # Asegurar que queda algo legible
                                lines.append(remaining_words[:cut_point] + "...")
                            else:
                                lines.append(remaining_words[:chars_per_line - 6] + "...")
                        else:
                            lines.append(remaining_words[:chars_per_line - 6] + "...")
                    else:
                        lines.append(remaining_words)
                    break

            # Agregar palabra a línea actual
            current_line.append(word)
            current_length += word_len + space_len

        # Agregar última línea si no llegamos al límite
        if current_line and len(lines) < 3:
            lines.append(' '.join(current_line))

        # Devolver resultado
        if len(lines) == 1:
            return lines[0]
        elif len(lines) == 2:
            return lines
        else:  # 3 líneas
            return lines


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