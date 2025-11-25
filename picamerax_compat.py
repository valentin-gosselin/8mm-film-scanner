"""
Wrapper de compatibilité picamerax -> picamera2
Ce module fournit une interface compatible avec picamerax en utilisant picamera2
"""

from picamera2 import Picamera2
from fractions import Fraction
from io import BytesIO
import time
import numpy as np
from PIL import Image


class PiCamera:
    """Wrapper de compatibilité pour PiCamera utilisant picamera2"""

    def __init__(self, resolution=(800, 600)):
        self.picam2 = Picamera2()
        self._resolution = resolution
        self._analog_gain = 1.0
        self._digital_gain = 1.0
        self._shutter_speed = 30000  # microseconds (30ms pour plus de lumière)
        self._awb_mode = "auto"
        self._awb_gains = (1.0, 1.0)
        self._vflip = False
        self._hflip = False

        # Flip logiciel pour l'affichage (indépendant du flip hardware)
        self._software_flip_h = False
        self._software_flip_v = False

        # Contrôles additionnels
        self._brightness = 0.0  # -1.0 à 1.0
        self._contrast = 1.0    # 0.0 à 32.0
        self._saturation = 1.0  # 0.0 à 32.0
        self._sharpness = 1.0   # 0.0 à 16.0

        # Configuration initiale
        config = self.picam2.create_still_configuration(
            main={"size": resolution, "format": "RGB888"},
            raw={"size": self.picam2.sensor_resolution}
        )
        self.picam2.configure(config)

    def __enter__(self):
        if not self.picam2.started:
            self.picam2.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    @property
    def resolution(self):
        return self._resolution

    @resolution.setter
    def resolution(self, value):
        self._resolution = value
        was_started = self.picam2.started
        if was_started:
            self.picam2.stop()

        config = self.picam2.create_still_configuration(
            main={"size": value, "format": "RGB888"},
            raw={"size": self.picam2.sensor_resolution}
        )
        self.picam2.configure(config)

        if was_started:
            self.picam2.start()

    @property
    def analog_gain(self):
        return self._analog_gain

    @analog_gain.setter
    def analog_gain(self, value):
        self._analog_gain = value
        self._update_controls()

    @property
    def digital_gain(self):
        return self._digital_gain

    @digital_gain.setter
    def digital_gain(self, value):
        self._digital_gain = value
        self._update_controls()

    @property
    def shutter_speed(self):
        return self._shutter_speed

    @shutter_speed.setter
    def shutter_speed(self, value):
        # picamerax utilise des microsecondes
        self._shutter_speed = value
        self._update_controls()

    @property
    def awb_mode(self):
        return self._awb_mode

    @awb_mode.setter
    def awb_mode(self, value):
        self._awb_mode = value
        self._update_controls()

    @property
    def awb_gains(self):
        return self._awb_gains

    @awb_gains.setter
    def awb_gains(self, value):
        # Convertir les Fraction en float si nécessaire
        if isinstance(value, (tuple, list)) and len(value) == 2:
            gains = (float(value[0]), float(value[1]))
            self._awb_gains = gains
            self._update_controls()

    @property
    def vflip(self):
        return self._vflip

    @vflip.setter
    def vflip(self, value):
        self._vflip = value
        self._update_transform()

    @property
    def hflip(self):
        return self._hflip

    @hflip.setter
    def hflip(self, value):
        self._hflip = value
        self._update_transform()

    @property
    def zoom(self):
        # picamera2 utilise ScalerCrop
        return (0.0, 0.0, 1.0, 1.0)

    @zoom.setter
    def zoom(self, value):
        # value = (x, y, w, h) normalized
        if not self.picam2.started:
            self.picam2.start()

        sensor_size = self.picam2.sensor_resolution
        x = int(value[0] * sensor_size[0])
        y = int(value[1] * sensor_size[1])
        w = int(value[2] * sensor_size[0])
        h = int(value[3] * sensor_size[1])

        self.picam2.set_controls({"ScalerCrop": (x, y, w, h)})

    @property
    def brightness(self):
        return self._brightness

    @brightness.setter
    def brightness(self, value):
        self._brightness = float(value)
        self._update_controls()

    @property
    def contrast(self):
        return self._contrast

    @contrast.setter
    def contrast(self, value):
        self._contrast = float(value)
        self._update_controls()

    @property
    def saturation(self):
        return self._saturation

    @saturation.setter
    def saturation(self, value):
        self._saturation = float(value)
        self._update_controls()

    @property
    def sharpness(self):
        return self._sharpness

    @sharpness.setter
    def sharpness(self, value):
        self._sharpness = float(value)
        self._update_controls()

    @property
    def software_flip_h(self):
        return self._software_flip_h

    @software_flip_h.setter
    def software_flip_h(self, value):
        self._software_flip_h = bool(value)

    @property
    def software_flip_v(self):
        return self._software_flip_v

    @software_flip_v.setter
    def software_flip_v(self, value):
        self._software_flip_v = bool(value)

    def _apply_software_flip(self, img):
        """Applique les flips logiciels à l'image"""
        if self._software_flip_h and self._software_flip_v:
            # Les deux flips = rotation 180°
            return img.rotate(180)
        elif self._software_flip_h:
            return img.transpose(Image.FLIP_LEFT_RIGHT)
        elif self._software_flip_v:
            return img.transpose(Image.FLIP_TOP_BOTTOM)
        return img

    def _update_controls(self):
        """Met à jour les contrôles de la caméra"""
        if not self.picam2.started:
            return

        try:
            controls = {}

            # Exposition manuelle
            if self._shutter_speed > 0:
                controls["ExposureTime"] = self._shutter_speed
                controls["AnalogueGain"] = self._analog_gain

            # Balance des blancs
            if self._awb_mode == "off" and self._awb_gains != (1.0, 1.0):
                controls["ColourGains"] = self._awb_gains

            # Contrôles additionnels
            controls["Brightness"] = self._brightness
            controls["Contrast"] = self._contrast
            controls["Saturation"] = self._saturation
            controls["Sharpness"] = self._sharpness

            if controls:
                self.picam2.set_controls(controls)
        except Exception as e:
            # Ne pas casser le stream si set_controls échoue
            print(f"Warning: Could not update camera controls: {e}")

    def _update_transform(self):
        """Met à jour la transformation (flip)"""
        if not self.picam2.started:
            return

        hflip = 1 if self._hflip else 0
        vflip = 1 if self._vflip else 0

        try:
            self.picam2.set_controls({
                "HorizontalFlip": hflip,
                "VerticalFlip": vflip
            })
        except RuntimeError as e:
            # Hardware flip not supported on this camera, use software flip instead
            print(f"Hardware flip not supported, using software flip: {e}")
            self._software_flip_h = self._hflip
            self._software_flip_v = self._vflip

    def capture(self, output, format='jpeg', bayer=False, use_video_port=False):
        """
        Capture une image

        Args:
            output: BytesIO ou chemin de fichier
            format: format de sortie ('jpeg', 'png', etc.)
            bayer: Si True, ajoute les données RAW bayer
            use_video_port: Si True, utilise le port vidéo (capture rapide)
        """
        if not self.picam2.started:
            self.picam2.start()
            time.sleep(0.5)

        # Attendre que la caméra soit stable
        time.sleep(0.1)

        if bayer:
            # Capture avec raw
            request = self.picam2.capture_request()
            try:
                # Image principale (JPEG)
                main_array = request.make_array("main")
                img = Image.fromarray(main_array)

                # Encoder en JPEG
                jpeg_buffer = BytesIO()
                img.save(jpeg_buffer, format='JPEG', quality=95)
                jpeg_data = jpeg_buffer.getvalue()

                # Données RAW (bayer)
                try:
                    raw_array = request.make_array("raw")
                    raw_data = raw_array.tobytes()
                except:
                    # Fallback si pas de RAW disponible
                    raw_data = b'\x00' * (4056 * 3040)  # Taille typique pour HQ Camera

                # Combiner JPEG + RAW
                combined_data = jpeg_data + raw_data

                if isinstance(output, BytesIO):
                    output.write(combined_data)
                else:
                    with open(output, 'wb') as f:
                        f.write(combined_data)

            finally:
                request.release()
        else:
            # Capture simple sans RAW
            if use_video_port:
                # Capture rapide depuis le flux vidéo
                array = self.picam2.capture_array("main")
                img = Image.fromarray(array)
            else:
                # Capture de qualité
                array = self.picam2.capture_array("main")
                img = Image.fromarray(array)

            # Appliquer les flips logiciels configurés
            img = self._apply_software_flip(img)

            if isinstance(output, BytesIO):
                img.save(output, format=format.upper(), quality=95)
            else:
                img.save(output, format=format.upper(), quality=95)

    def capture_continuous(self, output, format='jpeg', use_video_port=True):
        """
        Générateur de capture continue

        Yields:
            Pour chaque frame capturée
        """
        if not self.picam2.started:
            self.picam2.start()
            time.sleep(0.5)

        while True:
            try:
                # Utiliser capture_array avec timeout pour éviter les blocages
                array = self.picam2.capture_array("main")

                # Appliquer les contrôles APRÈS la capture pour ne pas bloquer
                # Les contrôles seront pris en compte à la frame suivante

                img = Image.fromarray(array)

                # Appliquer les flips logiciels configurés
                img = self._apply_software_flip(img)

                if isinstance(output, BytesIO):
                    output.seek(0)
                    output.truncate()
                    img.save(output, format=format.upper(), quality=85)

                yield output

            except Exception as e:
                print(f"Erreur dans capture_continuous: {e}")
                # Ne pas break, continuer à essayer
                time.sleep(0.1)
                continue

    def close(self):
        """Ferme la caméra"""
        if self.picam2.started:
            self.picam2.stop()
        self.picam2.close()

    def start_preview(self):
        """Démarre le preview (non implémenté pour picamera2)"""
        pass

    def stop_preview(self):
        """Arrête le preview (non implémenté pour picamera2)"""
        pass
