import os
import time
import json
import logging
import traceback
import sys
from flask import Flask, render_template, request, jsonify
from PIL import Image, ImageOps, ImageEnhance, ImageFilter

# Configuration du logging pour voir les erreurs sur Render
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

try:
    import vtracer
    _HAS_VTRACER = True
    logger.info("VTRACER : Chargé avec succès.")
except Exception as e:
    vtracer = None
    _HAS_VTRACER = False
    logger.error(f"VTRACER : Échec du chargement -> {str(e)}")

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024 
UPLOAD_FOLDER = os.path.join('static', 'outputs')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'bmp', 'tiff'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def cleanup_old_files():
    now = time.time()
    cutoff = now - (15 * 60) # 15 minutes
    try:
        for filename in os.listdir(UPLOAD_FOLDER):
            if filename == ".gitkeep": continue
            file_path = os.path.join(UPLOAD_FOLDER, filename)
            if os.path.isfile(file_path) and os.path.getmtime(file_path) < cutoff:
                os.remove(file_path)
                logger.info(f"Nettoyage : {filename} supprimé.")
    except Exception as e:
        logger.error(f"Erreur nettoyage : {e}")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/process', methods=['POST'])
def process_image():
    cleanup_old_files()
    
    try:
        # 1. Vérification du fichier
        if 'file' not in request.files:
            return jsonify({"error": "Aucun fichier reçu"}), 400
        
        file = request.files['file']
        params = json.loads(request.form.get('settings', '{}'))
        output_format = params.get('format', 'png').lower()
        
        logger.info(f"Début traitement : {file.filename} vers {output_format}")

        timestamp = int(time.time())
        input_path = os.path.join(UPLOAD_FOLDER, f"raw_{timestamp}_{file.filename}")
        output_filename = f"USI_{timestamp}.{output_format}"
        output_path = os.path.join(UPLOAD_FOLDER, output_filename)

        file.save(input_path)

        # 2. Traitement Image (Pillow)
        img = Image.open(input_path)
        img = ImageOps.exif_transpose(img)
        if img.mode != 'RGBA': img = img.convert('RGBA')

        # Application des filtres de base (B/C/S)
        alpha = img.getchannel('A')
        rgb = img.convert('RGB')
        rgb = ImageEnhance.Brightness(rgb).enhance(float(params.get('brightness', 1)))
        rgb = ImageEnhance.Contrast(rgb).enhance(float(params.get('contrast', 1)))
        rgb = ImageEnhance.Color(rgb).enhance(float(params.get('saturation', 1)))
        img = rgb.convert('RGBA')
        img.putalpha(alpha)

        # 3. CAS SPÉCIFIQUE : SVG (VTRACER)
        if output_format == 'svg':
            if not _HAS_VTRACER:
                return jsonify({"error": "Moteur SVG non installé sur le serveur"}), 500
            
            logger.info("Lancement de la vectorisation vtracer...")
            
            # Sauvegarde d'un PNG temporaire pour la vectorisation
            temp_png = os.path.join(UPLOAD_FOLDER, f"vtrace_tmp_{timestamp}.png")
            img.save(temp_png, "PNG")
            
            try:
                # Paramètres optimisés pour éviter le crash RAM sur Render
                vtracer.convert_image_to_svg_py(
                    temp_png,
                    output_path,
                    colortype = "color", 
                    mode = "spline",
                    filter_speckle = 4,
                    color_precision = 6, # Réduit de 8 à 6 pour économiser la RAM
                    layer_difference = 16
                )
                logger.info("Vectorisation réussie.")
            except Exception as ve:
                logger.error(f"CRASH VTRACER : {traceback.format_exc()}")
                return jsonify({"error": f"Erreur interne vtracer : {str(ve)}"}), 500
            finally:
                if os.path.exists(temp_png): os.remove(temp_png)

        # 4. AUTRES FORMATS (RASTER)
        else:
            if output_format in ['jpg', 'jpeg', 'ico']:
                bg = Image.new('RGB', img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[3])
                img = bg
            
            img.save(output_path, quality=90)
            logger.info(f"Export raster {output_format} réussi.")

        # Nettoyage et réponse
        if os.path.exists(input_path): os.remove(input_path)
        
        size_kb = os.path.getsize(output_path) // 1024
        return jsonify({
            "url": f"/static/outputs/{output_filename}",
            "name": output_filename,
            "size": f"{size_kb} KB"
        })

    except Exception as e:
        logger.error(f"ERREUR GLOBALE : {traceback.format_exc()}")
        return jsonify({"error": "Erreur serveur. Consultez les logs Render."}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
