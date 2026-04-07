import os
import time
import json
from flask import Flask, render_template, request, jsonify
from PIL import Image, ImageOps, ImageEnhance, ImageFilter
try:
    import vtracer
    _HAS_VTRACER = True
except Exception:
    vtracer = None
    _HAS_VTRACER = False

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # Augmenté à 32 Mo pour les pros
UPLOAD_FOLDER = os.path.join('static', 'outputs')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'bmp', 'tiff'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/')
def index():
    return render_template('index.html')
@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
    return response
@app.route('/process', methods=['POST'])
def process_image():
    
    # Nettoyage opportun du cache
    cleanup_old_files()

    try:
        if 'file' not in request.files:
            return jsonify({"error": "Aucun fichier détecté"}), 400

        file = request.files['file']
        if file.filename == '' or not allowed_file(file.filename):
            return jsonify({"error": "Format de fichier non supporté"}), 400

        params = json.loads(request.form.get('settings', '{}'))
        output_format = params.get('format', 'png').lower()

        timestamp = int(time.time())
        input_filename = f"raw_{timestamp}_{file.filename}"
        input_path = os.path.join(UPLOAD_FOLDER, input_filename)
        output_filename = f"USI_Converto_{timestamp}.{output_format}"
        output_path = os.path.join(UPLOAD_FOLDER, output_filename)

        file.save(input_path)

        # Load and normalize image
        img = Image.open(input_path)
        img = ImageOps.exif_transpose(img)
        if img.mode != 'RGBA':
            img = img.convert('RGBA')

        # Geometrie: resize
        req_w = params.get('resizeW')
        req_h = params.get('resizeH')
        if req_w or req_h:
            orig_w, orig_h = img.size
            if req_w and not req_h:
                new_w = int(req_w)
                new_h = int(orig_h * (new_w / orig_w))
            elif req_h and not req_w:
                new_h = int(req_h)
                new_w = int(orig_w * (new_h / orig_h))
            else:
                new_w, new_h = int(req_w), int(req_h)
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

        # Rotation
        try:
            rot = int(float(params.get('rotate', 0)))
        except Exception:
            rot = 0
        if rot:
            img = img.rotate(rot, expand=True)

        # Flip
        if params.get('flipH'): img = ImageOps.mirror(img)
        if params.get('flipV'): img = ImageOps.flip(img)

        # Grayscale
        if params.get('grayscale'):
            alpha = img.getchannel('A') if 'A' in img.getbands() else None
            img = img.convert('L').convert('RGBA')
            if alpha:
                img.putalpha(alpha)

        # Invert
        if params.get('invert'):
            r,g,b,a = img.split()
            inv = ImageOps.invert(Image.merge('RGB', (r,g,b)))
            r2,g2,b2 = inv.split()
            img = Image.merge('RGBA', (r2,g2,b2,a))

        # Color adjustments: do on RGB then reattach alpha
        alpha_chan = img.getchannel('A') if 'A' in img.getbands() else None
        base_rgb = img.convert('RGB')
        if float(params.get('brightness', 1)) != 1:
            base_rgb = ImageEnhance.Brightness(base_rgb).enhance(float(params['brightness']))
        if float(params.get('contrast', 1)) != 1:
            base_rgb = ImageEnhance.Contrast(base_rgb).enhance(float(params['contrast']))
        if float(params.get('saturation', 1)) != 1:
            base_rgb = ImageEnhance.Color(base_rgb).enhance(float(params['saturation']))
        if float(params.get('sharpness', 1)) != 1:
            base_rgb = ImageEnhance.Sharpness(base_rgb).enhance(float(params['sharpness']))
        img = base_rgb.convert('RGBA')
        if alpha_chan:
            img.putalpha(alpha_chan)

        # Blur (last)
        if float(params.get('blur', 0)) != 0:
            img = img.filter(ImageFilter.GaussianBlur(radius=float(params['blur'])))

        # If SVG requested, vectorize a prepared PNG so raster effects are baked in
       # --- BLOC SVG SÉCURISÉ POUR RENDER ---
        if output_format == 'svg':
            if not _HAS_VTRACER:
                return jsonify({"error": "Moteur SVG non disponible sur le serveur"}), 500

            temp_png = os.path.join(UPLOAD_FOLDER, f"temp_vtrace_{timestamp}.png")
            img.save(temp_png, format='PNG')
            
            try:
                # Utilisation des paramètres par défaut si absents des params
                vtracer.convert_image_to_svg_py(
                    temp_png,
                    output_path,
                    colortype = params.get('svgColorMode', 'color'),
                    mode = params.get('svgCurveMode', 'spline'),
                    filter_speckle = int(params.get('svgSpeckle', 4)),
                    color_precision = int(params.get('svgColorPrec', 6)), # Précision 6 est plus stable que 8 sur Render
                    layer_difference = int(params.get('svgLayerDiff', 16))
                )
            except Exception as v_err:
                print(f"Erreur VTRACER : {str(v_err)}")
                return jsonify({"error": f"Erreur de vectorisation : {str(v_err)}"}), 500
            finally:
                if os.path.exists(temp_png):
                    os.remove(temp_png)

            # Calcul de la taille et réponse
            size_kb = os.path.getsize(output_path) // 1024
            return jsonify({
                "url": f"/static/outputs/{output_filename}", 
                "name": output_filename, 
                "size": f"{size_kb} KB"
            })
        # Raster export (PNG, WEBP, JPG, ICO...)
        save_args = {}
        img_to_save = img
        if output_format in ['jpg', 'jpeg', 'ico']:
            bg = Image.new('RGB', img.size, (255,255,255))
            bg.paste(img, mask=img.split()[3])
            img_to_save = bg
            if output_format in ['jpg', 'jpeg']:
                save_args['quality'] = int(params.get('qualityRaster', 95))
        else:
            if output_format == 'png':
                save_args['optimize'] = True
            if output_format == 'webp':
                save_args['quality'] = int(params.get('qualityRaster', 85))

        format_map = {'jpg': 'JPEG', 'jpeg': 'JPEG', 'png': 'PNG', 'webp': 'WEBP', 'ico': 'ICO'}
        final_format = format_map.get(output_format, output_format.upper())
        img_to_save.save(output_path, format=final_format, **save_args)

        # Cleanup upload
        if os.path.exists(input_path):
            try:
                os.remove(input_path)
            except Exception:
                pass

        size_kb = os.path.getsize(output_path) // 1024
        return jsonify({"url": f"/static/outputs/{output_filename}", "name": output_filename, "size": f"{size_kb} KB"})
    except Exception as e:
        print(f"--- ERREUR PROSTUDIO : {str(e)} ---")
        return jsonify({"error": f"Erreur critique : {str(e)}"}), 500
       
    except Exception as e:
        # Log de l'erreur pour le débogage sur ton Pentium
        print(f"--- ERREUR PROSTUDIO : {str(e)} ---")
        return jsonify({"error": f"Erreur critique : {str(e)}"}), 500
import datetime

# Configuration du cache
CACHE_LIFETIME_MINUTES = 15  # Les fichiers sont supprimés après 5 min

def cleanup_old_files():
    """Supprime les fichiers plus vieux que CACHE_LIFETIME_MINUTES dans le dossier outputs."""
    now = time.time()
    cutoff = now - (CACHE_LIFETIME_MINUTES * 60)
    
    try:
        for filename in os.listdir(UPLOAD_FOLDER):
            file_path = os.path.join(UPLOAD_FOLDER, filename)
            # Vérifier si c'est un fichier et s'il est trop vieux
            if os.path.isfile(file_path):
                if os.path.getmtime(file_path) < cutoff:
                    os.remove(file_path)
                    print(f"--- Cache : Nettoyage de {filename}")
    except Exception as e:
        print(f"Erreur lors du nettoyage : {e}")
if __name__ == '__main__':
    # Render utilise la variable d'environnement PORT
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
