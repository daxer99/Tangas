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

app = Flask(__name__)


class PaulinaScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })

    def scrape_product(self, url):
        try:
            print(f"🔍 Scraping URL: {url}")
            response = self.session.get(url, timeout=10)
            response.raise_for_status()

            soup = BeautifulSoup(response.content, 'html.parser')

            product_data = {
                'name': self.extract_name(soup),
                'price': self.extract_price(soup),
                'image_url': self.extract_image(soup, url),
                'sizes_colors': self.extract_sizes_and_colors(soup),
                'original_url': url
            }

            print(f"✅ Datos extraídos: {product_data}")
            return product_data

        except Exception as e:
            print(f"❌ Error en scraping: {e}")
            return {'error': str(e)}

    def extract_name(self, soup):
        """Extraer nombre del producto"""
        # Buscar en el formulario donde está la descripción
        desc_input = soup.find('input', {'name': 'descripcion'})
        if desc_input and desc_input.get('value'):
            name = desc_input['value'].strip()
            if name:
                print(f"✅ Nombre desde input descripcion: {name}")
                return name

        # Buscar en elementos de texto
        selectors = [
            'h3', 'h1', '.product-title', '.product-name'
        ]

        for selector in selectors:
            try:
                element = soup.select_one(selector)
                if element and element.text.strip():
                    name = element.text.strip()
                    print(f"✅ Nombre encontrado con selector '{selector}': {name}")
                    return name
            except Exception as e:
                continue

        return "Producto Paulina Mayorista"

    def extract_price(self, soup):
        """Extraer precio del producto"""
        # Buscar en el input hidden del precio
        price_input = soup.find('input', {'name': 'precio'})
        if price_input and price_input.get('value'):
            try:
                price = float(price_input['value'])
                print(f"✅ Precio desde input: {price}")
                return price
            except ValueError:
                pass

        # Buscar en texto de la página
        price_selectors = [
            '.title strong',
            'p.title strong',
            '.precio',
            '.price'
        ]

        for selector in price_selectors:
            try:
                elements = soup.select(selector)
                for element in elements:
                    price_text = element.text.strip()
                    matches = re.findall(r'\$?\s*(\d+[.,]\d+)', price_text)
                    if matches:
                        price_str = matches[0].replace(',', '.')
                        price = float(price_str)
                        print(f"✅ Precio desde texto: {price}")
                        return price
            except Exception as e:
                continue

        return 0.0

    def extract_image(self, soup, base_url):
        """Extraer imagen PRINCIPAL del producto - VERSIÓN CORREGIDA"""
        print("🖼️ Buscando imagen PRINCIPAL del producto...")

        # ESTRATEGIA 1: Buscar en la galería (donde están las imágenes del producto)
        gallery_selectors = [
            '.tz-gallery .col-sm-12.col-md-12 img.img-responsive',  # Imagen principal en galería
            '.tz-gallery img.img-responsive',  # Cualquier imagen responsive en galería
            '.tz-gallery img',  # Cualquier imagen en galería
            'a.lightbox img'  # Imágenes que abren lightbox
        ]

        for selector in gallery_selectors:
            try:
                img_elements = soup.select(selector)
                print(f"  Buscando con selector: {selector} - Encontradas: {len(img_elements)}")

                for img in img_elements:
                    img_src = img.get('src', '').strip()
                    if img_src:
                        full_url = self.make_absolute_url(img_src, base_url)
                        print(f"✅✅✅ IMAGEN PRINCIPAL ENCONTRADA: {full_url}")
                        return full_url
            except Exception as e:
                print(f"Error con selector {selector}: {e}")
                continue

        # ESTRATEGIA 2: Buscar imágenes específicas en uploads/products/
        all_images = soup.find_all('img')
        print(f"📸 Total de imágenes en página: {len(all_images)}")

        for i, img in enumerate(all_images):
            img_src = img.get('src', '')
            if img_src:
                print(f"  Imagen {i + 1}: {img_src}")

                # Filtrar solo imágenes de productos
                if 'uploads/products/' in img_src:
                    # Excluir thumbnails
                    if not any(thumb in img_src.lower() for thumb in ['thumb', 'small', 'mini']):
                        full_url = self.make_absolute_url(img_src, base_url)
                        print(f"✅ Imagen de producto encontrada: {full_url}")
                        return full_url

        print("❌ No se pudo encontrar la imagen del producto")
        return None

    def extract_sizes_and_colors(self, soup):
        """Extraer talles y colores disponibles - VERSIÓN CORREGIDA"""
        print("🎨 Extrayendo talles y colores...")

        sizes_colors_data = {
            'sizes': [],
            'colors': [],
            'availability': {}
        }

        try:
            table = soup.find('table')
            if not table:
                return sizes_colors_data

            # 1. TALLES - Buscar th en thead
            thead = table.find('thead')
            if thead:
                th_elements = thead.find_all('th')[1:]  # Saltar primer th vacío
                for th in th_elements:
                    size = th.get_text(strip=True)
                    if size:
                        sizes_colors_data['sizes'].append(size)

            # Si no hay talles, usar UNICO
            if not sizes_colors_data['sizes']:
                sizes_colors_data['sizes'] = ['UNICO']

            # 2. COLORES - Buscar TODOS los spans en tbody
            tbody = table.find('tbody')
            if tbody:
                # Buscar TODOS los spans en el tbody (cada fila tiene uno)
                color_spans = tbody.find_all('span')
                print(f"🎨 Se encontraron {len(color_spans)} spans de colores")

                for span in color_spans:
                    color_name = span.get_text(strip=True)
                    if color_name and color_name not in sizes_colors_data['colors']:
                        sizes_colors_data['colors'].append(color_name)
                        sizes_colors_data['availability'][color_name] = {}

                        # Marcar disponibilidad para todos los talles
                        for size in sizes_colors_data['sizes']:
                            sizes_colors_data['availability'][color_name][size] = True

            print(f"✅ RESULTADO: {len(sizes_colors_data['colors'])} colores → {sizes_colors_data['colors']}")
            print(f"✅ RESULTADO: {len(sizes_colors_data['sizes'])} talles → {sizes_colors_data['sizes']}")

        except Exception as e:
            print(f"❌ Error: {e}")

        return sizes_colors_data

    def make_absolute_url(self, img_src, base_url):
        """Convertir URL relativa a absoluta"""
        if img_src.startswith('//'):
            return 'https:' + img_src
        elif img_src.startswith('/'):
            parsed_url = urllib.parse.urlparse(base_url)
            return f"{parsed_url.scheme}://{parsed_url.netloc}{img_src}"
        elif img_src.startswith('http'):
            return img_src
        else:
            # Para URLs relativas como "uploads/products/LC7326.Ijpg.jpg"
            parsed_url = urllib.parse.urlparse(base_url)
            base_domain = f"{parsed_url.scheme}://{parsed_url.netloc}"

            if img_src.startswith('/'):
                return f"{base_domain}{img_src}"
            else:
                return f"{base_domain}/{img_src}"


class ImageGenerator:
    def generate_product_image(self, product_data, price_formula="x * 1.3"):
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
                    draw.text((x + col_width / 2, y + row_height / 2), symbol,
                              fill=text_color, font=cell_font, anchor="mm")

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
                response = requests.get(image_url, timeout=15)
                response.raise_for_status()

                # Verificar que sea una imagen
                content_type = response.headers.get('content-type', '')
                if 'image' not in content_type:
                    print(f"❌ URL no es una imagen: {content_type}")
                    return self.create_placeholder()

                image = Image.open(io.BytesIO(response.content))
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
        """Cargar fuentes con tamaños fijos grandes"""
        try:
            # TAMAÑOS FIJOS GRANDES (igual en local y en servidor)
            title_font_size = 36  # Título grande
            price_font_size = 52  # Precio muy grande
            table_font_size = 14  # Tabla normal

            # Intentar diferentes fuentes
            font_loaded = False
            font = None

            # Lista de fuentes a probar (en orden de preferencia)
            font_paths = [
                "arial.ttf",
                "DejaVuSans.ttf",
                "LiberationSans-Regular.ttf"
            ]

            for font_path in font_paths:
                try:
                    font = ImageFont.truetype(font_path, title_font_size)
                    font_loaded = True
                    print(f"✅ Fuente cargada: {font_path}")
                    break
                except:
                    continue

            if font_loaded:
                title_font = ImageFont.truetype(font_path, title_font_size)
                price_font = ImageFont.truetype(font_path, price_font_size)
                table_font = ImageFont.truetype(font_path, table_font_size)
            else:
                # Fuentes por defecto con tamaños aumentados
                print("⚠️  Usando fuentes por defecto - aumentando tamaño")
                title_font = ImageFont.load_default()
                price_font = ImageFont.load_default()
                table_font = ImageFont.load_default()
                # Aumentar significativamente para fuentes por defecto
                title_font_size = 50
                price_font_size = 70

            print(f"🎯 Tamaños finales - Título: {title_font_size}px, Precio: {price_font_size}px")

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
            return self.smart_round_price(original_price * 1.3, "x * 1.3")

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
        """Dibujar textos en la imagen con posiciones dinámicas y texto ajustado"""
        product_x, product_y = product_position
        product_width, product_height = product_size

        # Calcular posición Y para los textos (debajo de la imagen)
        text_start_y = product_y + product_height + 30

        # Dividir el nombre en líneas si es muy largo
        wrapped_lines = self.wrap_text(name, title_font, canvas_width - 100)

        # Dibujar nombre del producto (puede ser multilínea)
        if isinstance(wrapped_lines, list):
            # Texto multilínea
            line_height = 35  # Espacio entre líneas
            total_text_height = len(wrapped_lines) * line_height

            for i, line in enumerate(wrapped_lines):
                y_position = text_start_y + (i * line_height)
                draw.text((canvas_width // 2, y_position), line,
                          fill='black', font=title_font, anchor="mm")

            # Posición del precio después del nombre multilínea
            price_y = text_start_y + total_text_height + 20
        else:
            # Texto de una línea
            draw.text((canvas_width // 2, text_start_y), wrapped_lines,
                      fill='black', font=title_font, anchor="mm")
            price_y = text_start_y + 50

        # Dibujar precio
        price_text = f"${price:.2f}"
        draw.text((canvas_width // 2, price_y), price_text,
                  fill='red', font=price_font, anchor="mm")

    def wrap_text(self, text, font, max_width):
        """Dividir texto en múltiples líneas si es muy ancho - VERSIÓN MEJORADA"""
        # Si el texto es corto, devolver como está
        if len(text) <= 25:
            return text

        words = text.split()
        lines = []
        current_line = []

        for word in words:
            # Probar si la línea actual + nueva palabra cabe
            test_line = ' '.join(current_line + [word])

            # Estimación más precisa del ancho (basada en caracteres)
            estimated_width = len(test_line) * 8  # Ajuste más preciso

            if estimated_width <= max_width:
                current_line.append(word)
            else:
                if current_line:
                    lines.append(' '.join(current_line))
                current_line = [word]

                # Limitar a 3 líneas máximo
                if len(lines) >= 2:  # Ya tenemos 2 líneas, esta sería la tercera
                    remaining_words = ' '.join(current_line + words[words.index(word) + 1:])
                    if len(remaining_words) > 15:  # Si queda mucho texto
                        lines.append(remaining_words[:15] + "...")
                    else:
                        lines.append(remaining_words)
                    break

        if current_line and len(lines) < 3:
            lines.append(' '.join(current_line))

        # Si solo hay una línea, devolver como string
        if len(lines) == 1:
            return lines[0]
        else:
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
    product_data = scraper.scrape_product(url)
    return jsonify({'success': True, 'debug_data': product_data})


@app.route('/generate-image', methods=['POST'])
def generate_image():
    data = request.json
    url = data.get('url')
    formula = data.get('formula', 'x * 1.3')

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
            'product_data': product_data
        })
    else:
        return jsonify({'success': False, 'error': 'Error generando imagen'})


@app.route('/download/<image_id>')
def download_file(image_id):
    try:
        # Obtener parámetros de la URL
        product_url = request.args.get('url')
        formula = request.args.get('formula', 'x * 1.3')

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