
TV STREAMER V2
===============

BESCHRIJVING
------------
TV Streamer maakt van een map met videobestanden
een doorlopende televisiezender via het netwerk.

Het programma gebruikt FFmpeg voor het afspelen
en omzetten van de videobestanden.

Meerdere clients kunnen tegelijkertijd kijken
naar dezelfde uitzending.

BENODIGDHEDEN
-------------
- Python 3.9 of hoger
- FFmpeg
- FFmpeg moet beschikbaar zijn via PATH

INSTALLATIE
-----------
1. Pak alle bestanden uit in een map.
2. Installeer Python.
3. Installeer FFmpeg.
4. Controleer of FFmpeg werkt met:

   ffmpeg -version

5. Plaats videobestanden in de map videos.
6. Pas eventueel settings.txt aan.

STARTEN
-------
Windows:
Dubbelklik op start.bat.

Of gebruik:

python tv_streamer.py

Linux:
python3 tv_streamer.py

UITVOERMODI
-----------
In settings.txt kan output_mode worden ingesteld.

web:
Alleen browserstream via HLS.

stream:
Alleen MPEG-TS voor VLC/IPTV-clients.

both:
Browserstream en MPEG-TS tegelijkertijd.

STREAMADRESSEN
--------------
Browser:
http://SERVER-IP:8080/

HLS:
http://SERVER-IP:8080/hls/stream.m3u8

MPEG-TS:
http://SERVER-IP:8080/tv

VIDEO'S
-------
Ondersteunde bestandstypen:

MP4
MKV
AVI
MOV
M4V
TS
WEBM
MPEG
MPG
WMV
FLV
3GP

BESTANDSWEERGAVE
----------------
De bestanden worden alfabetisch afgespeeld.

Na het laatste bestand begint de uitzending
automatisch opnieuw bij het eerste bestand.

Nieuwe kijkers sluiten aan op het actuele
uitzendmoment.

INSTELLINGEN
------------
folder:
Locatie van de videobestanden.

port:
Netwerkpoort van de server.

width / height:
Uitvoerresolutie.

video_bitrate:
Videokwaliteit.

audio_bitrate:
Audiokwaliteit.

fps:
Beeldfrequentie.

hls_time:
Lengte van HLS-segmenten in seconden.

hls_list_size:
Aantal segmenten in de HLS-playlist.

hls_dir:
Map waarin HLS-bestanden worden opgeslagen.

BELANGRIJK
----------
Bij wijzigingen aan settings.txt moet het
programma opnieuw gestart worden.

Nieuwe of verwijderde videobestanden worden
bij een nieuwe start van FFmpeg in de playlist
opgenomen.

Transcoderen vraagt processorkracht.

Verlaag de resolutie of bitrate als de server
te zwaar wordt belast.

Zorg dat de ingestelde TCP-poort openstaat
in de firewall.

De server heeft standaard geen authenticatie.
Gebruik hem alleen op een vertrouwd netwerk.

VEEL KIJKPLEZIER!
