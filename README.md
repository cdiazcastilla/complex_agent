# Asistente de voz para adultos mayores

Un asistente que vive en un celular Android, **siempre escuchando**, y que la persona controla solo con la voz:

> "Oye asistente… ¿ya me tomé la pastilla de la presión?"
> "Oye asistente, márcale a mi sobrino."
> "Oye asistente, acuérdate que guardé las escrituras en el ropero."
> "¡Ayuda!"

## Qué hace

| Función | Cómo |
|---|---|
| **Emergencias** | Si dice "ayuda", "auxilio", "me caí"… avisa al instante a la familia (Telegram/WhatsApp). Si oye "ayuda" sin la palabra de activación, pregunta "¿Está bien?" y, si nadie contesta, avisa igual. Sin internet manda un **SMS**. También hay un botón rojo de **AYUDA** en la pantalla. |
| **Medicinas** | A cada hora le dice qué tomar y pregunta si ya se la tomó. Si no confirma en 30 min, **avisa a la familia**. |
| **Llamadas** | "Márcale a Lupita" → marca al contacto guardado. |
| **Recados** | "Dile a mi sobrino que venga el domingo" → le llega el mensaje a la familia. |
| **Memoria** | Notas ("dejé las llaves en…"), citas con recordatorio un día y dos horas antes, recordatorios sueltos ("recuérdame a las 5 regar las plantas"). |
| **Compañía** | Platica de lo que quiera, le da la fecha, la hora y el clima. |
| **Vigilancia** | Si el celular deja de reportarse por 1 hora (apagado, sin batería, sin internet), avisa a la familia. |
| **Panel familiar** | Página web para dar de alta medicinas, contactos y citas, y para ver si se tomó las medicinas y el historial. |

## Cómo funciona

```
 Celular Android (siempre escuchando)                    Servidor (Python)
 ┌──────────────────────────────────────┐               ┌─────────────────────────────┐
 │ Vosk, sin internet: espera "oye      │               │ Claude + herramientas:      │
 │ asistente" o "ayuda"                 │               │ medicinas, citas, notas,    │
 │        ↓                             │   texto       │ contactos, avisos, clima    │
 │ Reconocedor de Android: transcribe   │ ───────────▶  │                             │
 │        ↓                             │               │ Revisa cada 30 s: horas de  │
 │ Texto a voz: dice la respuesta       │ ◀───────────  │ medicinas, recordatorios,   │
 │ y puede llamar o mandar SMS          │   respuesta   │ avisos a la familia         │
 └──────────────────────────────────────┘               └──────────┬──────────────────┘
                                                                    │ Telegram / WhatsApp
                                                                    ▼
                                                              Familia + panel web
```

- **La palabra de activación se detecta dentro del celular.** Nada sale a internet hasta que la escucha.
- El servidor guarda todo en una base SQLite y es el que decide cuándo recordar cosas, así que las medicinas siguen funcionando aunque se reinicie el celular.

```
server/    servidor en Python (FastAPI + API de Claude)
android/   app Android en Kotlin
```

---

## 1. Poner el servidor

Necesitas una **llave de la API de Claude** ([console.anthropic.com](https://console.anthropic.com)) y un lugar donde el servidor esté prendido siempre: una compu de la casa, una Raspberry Pi o un servicio en la nube (Railway, Render, Fly.io…; dale un disco persistente montado en `/datos`).

```bash
cd server
cp .env.example .env        # y llénalo
```

**Con Docker:**
```bash
docker build -t asistente server
docker run -d --restart=always --env-file server/.env -v asistente-datos:/datos -p 8000:8000 asistente
```

**Sin Docker:**
```bash
cd server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
set -a; source .env; set +a
.venv/bin/uvicorn app.main:app --factory --host 0.0.0.0 --port 8000
```

El celular tiene que poder llegar al servidor. En la misma red WiFi basta con `http://IP-de-la-compu:8000`. Desde fuera de la casa usa HTTPS (un servicio en la nube, o un túnel como Cloudflare Tunnel o Tailscale).

### Avisos a la familia por Telegram (recomendado, gratis)

1. En Telegram, háblale a **@BotFather**, manda `/newbot` y copia el token en `TELEGRAM_BOT_TOKEN`.
2. Crea un grupo "Familia", agrega al bot y manda cualquier mensaje en el grupo.
3. Abre `https://api.telegram.org/bot<TOKEN>/getUpdates` y copia el `chat.id` del grupo (empieza con `-`) en `TELEGRAM_CHAT_IDS`.

### O por WhatsApp (CallMeBot, gratis)

Cada familiar sigue los pasos de [callmebot.com](https://www.callmebot.com/blog/free-api-whatsapp-messages/) para obtener su apikey, y se ponen así: `CALLMEBOT_WHATSAPP=5215511112222:123456,5215533334444:654321`.

### Panel familiar

Abre `http://tu-servidor:8000/panel` y entra con el `APP_TOKEN`. Ahí das de alta **medicinas** (con horas `08:00,20:00`), **contactos** y **citas**.

---

## 2. Instalar la app en el celular

**APK ya compilado:** cada vez que se sube código, GitHub Actions lo compila. Entra a la pestaña **Actions** del repositorio, abre la última ejecución verde y baja `asistente-apk`. Pásalo al celular e instálalo (hay que permitir "instalar apps de origen desconocido").

**O compílalo tú:** abre la carpeta `android/` en Android Studio y dale ▶️.

### Configurar el celular

1. Abre la app, toca **Configuración** y llena: dirección del servidor, contraseña (`APP_TOKEN`), palabra de activación y teléfono para SMS de emergencia.
2. Toca **Guardar y activar** y acepta los permisos: micrófono, llamadas, SMS y notificaciones. La primera vez baja el modelo de voz (~40 MB).
3. Toca **Permisos de batería y llamadas** dos veces: quita la optimización de batería y permite "mostrar sobre otras apps" (sin esto no puede marcar con la pantalla apagada).
4. **Muy importante en Xiaomi, Huawei, Oppo, Vivo y Samsung:** estas marcas matan las apps en segundo plano. Entra a Ajustes → Apps → Asistente y activa **Inicio automático**, pon la batería en **Sin restricciones** y, en Xiaomi, "bloquea" la app en recientes (candado). Guía por marca: [dontkillmyapp.com](https://dontkillmyapp.com).
5. Revisa que esté instalado **"Speech Services by Google"** (Servicios de voz de Google) y que tenga la voz en español de México.

### Recomendación: un celular dedicado

Lo ideal es un Android barato o uno viejo **solo para esto**, siempre conectado al cargador en la sala o la recámara. Así no hay problemas de batería y nadie lo mete a una bolsa donde no oye. Si usas el celular de alguien, **esa persona tiene que estar de acuerdo**: el micrófono queda encendido (aunque solo reacciona a la palabra de activación).

Si el celular se reinicia, aparece una notificación **"Toque aquí para activar el asistente"**. Android no deja abrir el micrófono solo después de prender.

### Sobre la palabra de activación

La detecta Vosk con un modelo pequeño en español. Las palabras tienen que existir en su vocabulario: frases comunes como "oye asistente" o "oye compañero" funcionan, pero un apodo raro puede no reconocerse. Si no despierta, prueba otra frase.

---

## Costos aproximados

- **Claude:** depende de cuánto platique. Usa `claude-opus-5-5` con esfuerzo bajo para contestar rápido. Puedes cambiarlo con `MODELO` y `ESFUERZO` en el `.env`, por ejemplo `MODELO=claude-haiku-5-5` para gastar mucho menos.
- **Telegram, CallMeBot, Vosk, el clima (Open-Meteo) y la voz de Android:** gratis.
- **Servidor:** gratis en una compu de la casa, o unos pocos dólares al mes en la nube.

## Límites importantes

- **No sustituye un botón de pánico médico ni al 911.** Es un apoyo; para emergencias graves siempre hay que llamar al 911.
- No da consejos médicos: recuerda, avisa y escala a la familia.
- Depende de internet para platicar. Sin internet, lo único que sigue funcionando son el botón de AYUDA y la detección de "ayuda", que avisan por SMS.

## Desarrollo

```bash
cd server
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```
