package mx.asistente.voz

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.media.AudioManager
import android.media.ToneGenerator
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.telephony.SmsManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import org.json.JSONArray
import org.json.JSONObject
import org.vosk.Model
import org.vosk.Recognizer
import org.vosk.android.SpeechService
import java.util.Locale
import java.util.UUID
import java.util.concurrent.Executors
import android.speech.RecognitionListener as OyenteAndroid
import org.vosk.android.RecognitionListener as OyenteVosk

/**
 * Servicio en primer plano que escucha todo el tiempo:
 *  1. Vosk (sin internet) espera la palabra de activación o "ayuda"/"auxilio".
 *  2. Al oírla, el reconocedor de Android transcribe lo que dice la persona.
 *  3. El texto va al servidor (Claude) y la respuesta se dice en voz alta.
 *  4. Sigue escuchando sin palabra de activación mientras la plática continúe.
 * Cada 30 s pregunta al servidor si hay recordatorios que decir.
 */
class AsistenteService : Service() {

    companion object {
        private const val TAG = "Asistente"
        const val ACCION_HABLAR = "mx.asistente.voz.HABLAR"
        const val ACCION_AYUDA = "mx.asistente.voz.AYUDA"
        const val ACCION_DETENER = "mx.asistente.voz.DETENER"
        private const val CANAL = "asistente"
        private const val ID_NOTIFICACION = 1
        private const val CADA_CUANDO_MENSAJES = 30_000L
        private val PALABRAS_AYUDA = listOf("ayuda", "auxilio")
        private val FRASES_EMERGENCIA = Regex(
            "\\b(ayuda|auxilio|me ca[ií]|me desmay|no puedo respirar|me duele el pecho)",
            RegexOption.IGNORE_CASE,
        )

        /** Último estado, para mostrarlo en pantalla. */
        @Volatile var estadoActual: String = "Apagado"
            private set
        /** La pantalla se suscribe aquí (siempre se llama en el hilo principal). */
        var oyente: ((String) -> Unit)? = null

        fun iniciar(context: Context, accion: String? = null) {
            ContextCompat.startForegroundService(
                context,
                Intent(context, AsistenteService::class.java).setAction(accion),
            )
        }
    }

    private enum class Fase { CARGANDO, ESPERANDO, ESCUCHANDO, PENSANDO, HABLANDO }
    private enum class Modo { NORMAL, CONFIRMAR_EMERGENCIA }

    private val principal = Handler(Looper.getMainLooper())
    private val red = Executors.newSingleThreadExecutor()
    private lateinit var ajustes: Ajustes
    private lateinit var servidor: Servidor

    private var iniciado = false
    private var fase = Fase.CARGANDO
    private var modo = Modo.NORMAL
    private var seguimiento = false
    /** Sube con cada interacción nueva, para ignorar respuestas que llegan tarde. */
    private var turno = 0

    private var modelo: Model? = null
    private var vosk: SpeechService? = null
    private var reconocedorVosk: Recognizer? = null
    private var reconocedor: SpeechRecognizer? = null
    private var tts: TextToSpeech? = null
    private var ttsListo = false
    private val alTerminarDeHablar = mutableMapOf<String, () -> Unit>()
    private var tonos: ToneGenerator? = null
    private var candado: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACCION_DETENER) {
            stopSelf()
            return START_NOT_STICKY
        }
        if (!iniciado) {
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                Log.w(TAG, "Sin permiso de micrófono; no se puede iniciar")
                stopSelf()
                return START_NOT_STICKY
            }
            val tipo = if (Build.VERSION.SDK_INT >= 30) ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else 0
            ServiceCompat.startForeground(this, ID_NOTIFICACION, notificacion("Iniciando…"), tipo)
            iniciado = true
            arrancar()
        }
        when (intent?.action) {
            ACCION_HABLAR -> if (fase == Fase.ESPERANDO) activar()
            ACCION_AYUDA -> {
                detenerTodoLoQueEscucha()
                enviarEmergencia("Apretó el botón de AYUDA en el celular")
            }
        }
        return START_STICKY
    }

    private fun arrancar() {
        ajustes = Ajustes(this)
        servidor = Servidor(ajustes)
        candado = (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "asistente:escucha")
            .apply { acquire() }
        tonos = try { ToneGenerator(AudioManager.STREAM_MUSIC, 90) } catch (e: RuntimeException) { null }
        mostrar("Cargando…")

        tts = TextToSpeech(this) { estado ->
            if (estado == TextToSpeech.SUCCESS) {
                val t = tts ?: return@TextToSpeech
                if (t.setLanguage(Locale.forLanguageTag("es-MX")) < TextToSpeech.LANG_AVAILABLE) {
                    t.setLanguage(Locale.forLanguageTag("es"))
                }
                t.setSpeechRate(ajustes.velocidadVoz)
                t.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                    override fun onStart(id: String) {}
                    override fun onDone(id: String) = terminoDeHablar(id)
                    @Deprecated("Requerido por la API") override fun onError(id: String) = terminoDeHablar(id)
                    override fun onError(id: String, codigo: Int) = terminoDeHablar(id)
                })
                principal.post { ttsListo = true; quizaListo() }
            } else {
                Log.e(TAG, "No hay motor de texto a voz")
                principal.post { mostrar("Falta el motor de voz (instale 'Speech Services by Google')") }
            }
        }

        Thread {
            try {
                val m = Model(ModeloVoz.carpeta(this).absolutePath)
                principal.post { modelo = m; quizaListo() }
            } catch (e: Exception) {
                Log.e(TAG, "No se pudo cargar el modelo de voz", e)
                principal.post { mostrar("Falta el modelo de voz: abra la app y toque Guardar") }
            }
        }.start()

        principal.postDelayed(revisarMensajes, 5_000)
    }

    private fun quizaListo() {
        if (fase == Fase.CARGANDO && ttsListo && modelo != null) esperarPalabra()
    }

    override fun onDestroy() {
        principal.removeCallbacksAndMessages(null)
        detenerTodoLoQueEscucha()
        reconocedor?.destroy()
        tts?.shutdown()
        modelo?.close()
        tonos?.release()
        red.shutdownNow()
        candado?.let { if (it.isHeld) it.release() }
        mostrar("Apagado")
        super.onDestroy()
    }

    // ---------- 1. Esperar la palabra de activación (Vosk, sin internet) ----------

    private fun esperarPalabra() {
        detenerTodoLoQueEscucha()
        modo = Modo.NORMAL
        val m = modelo
        if (m == null || !ttsListo) {
            fase = Fase.CARGANDO  // quizaListo() la llamará cuando termine de cargar
            return
        }
        fase = Fase.ESPERANDO
        mostrar("Diga \"${ajustes.palabra}\" para hablarme")
        try {
            val gramatica = JSONArray(listOf(ajustes.palabra) + PALABRAS_AYUDA + "[unk]").toString()
            val rec = Recognizer(m, 16000f, gramatica)
            reconocedorVosk = rec
            vosk = SpeechService(rec, 16000f).also { it.startListening(oyenteVosk) }
        } catch (e: Exception) {
            Log.e(TAG, "No se pudo abrir el micrófono", e)
            mostrar("No se pudo abrir el micrófono; reintentando…")
            principal.postDelayed({ if (fase == Fase.ESPERANDO) esperarPalabra() }, 5_000)
        }
    }

    private val oyenteVosk = object : OyenteVosk {
        override fun onPartialResult(hipotesis: String) {
            // La palabra de activación se reconoce en cuanto se oye, para responder rápido.
            if (fase == Fase.ESPERANDO && texto(hipotesis, "partial").contains(ajustes.palabra)) activar()
        }

        override fun onResult(hipotesis: String) {
            if (fase != Fase.ESPERANDO) return
            val t = texto(hipotesis, "text")
            when {
                t.contains(ajustes.palabra) -> activar()
                PALABRAS_AYUDA.any { t.contains(it) } -> confirmarEmergencia()
            }
        }

        override fun onFinalResult(hipotesis: String) {}
        override fun onError(e: Exception) {
            Log.e(TAG, "Error de Vosk", e)
            principal.postDelayed({ if (fase == Fase.ESPERANDO) esperarPalabra() }, 2_000)
        }
        override fun onTimeout() {}
    }

    private fun texto(hipotesis: String, campo: String) =
        try { JSONObject(hipotesis).optString(campo) } catch (e: Exception) { "" }

    private fun detenerVosk() {
        vosk?.stop()
        vosk?.shutdown()
        vosk = null
        reconocedorVosk?.close()
        reconocedorVosk = null
    }

    private fun detenerTodoLoQueEscucha() {
        detenerVosk()
        reconocedor?.cancel()
    }

    // ---------- 2. Escuchar lo que dice (reconocedor de Android) ----------

    private fun activar() {
        detenerVosk()
        tono()
        escuchar(seguimiento = false)
    }

    private fun escuchar(seguimiento: Boolean) {
        detenerVosk()
        this.seguimiento = seguimiento
        fase = Fase.ESCUCHANDO
        mostrar(if (modo == Modo.CONFIRMAR_EMERGENCIA) "¿Está bien?" else "Lo escucho…")
        val r = reconocedor ?: SpeechRecognizer.createSpeechRecognizer(this).also {
            it.setRecognitionListener(oyenteAndroid)
            reconocedor = it
        }
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, "es-MX")
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
            // Más paciencia con las pausas al hablar.
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 2500L)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS, 2000L)
        }
        r.startListening(intent)
    }

    private val oyenteAndroid = object : OyenteAndroid {
        override fun onResults(resultados: Bundle) {
            if (fase != Fase.ESCUCHANDO) return
            val dicho = resultados.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull().orEmpty()
            if (dicho.isBlank()) noDijoNada() else procesar(dicho)
        }

        override fun onError(error: Int) {
            if (fase != Fase.ESCUCHANDO) return
            Log.i(TAG, "Reconocedor de Android: error $error")
            when (error) {
                SpeechRecognizer.ERROR_NO_MATCH, SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> noDijoNada()
                else -> {
                    reconocedor?.destroy()
                    reconocedor = null
                    if (modo == Modo.CONFIRMAR_EMERGENCIA) {
                        noDijoNada()
                    } else {
                        fase = Fase.PENSANDO
                        principal.postDelayed({ esperarPalabra() }, 1_000)
                    }
                }
            }
        }

        override fun onReadyForSpeech(params: Bundle?) {}
        override fun onBeginningOfSpeech() {}
        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {}
        override fun onPartialResults(parciales: Bundle?) {}
        override fun onEvent(eventType: Int, params: Bundle?) {}
    }

    private fun noDijoNada() {
        when {
            // Oímos "ayuda" y nadie contestó: más vale avisar.
            modo == Modo.CONFIRMAR_EMERGENCIA ->
                enviarEmergencia("Se escuchó que alguien pidió ayuda y luego nadie contestó")
            seguimiento -> esperarPalabra()
            else -> decir("No le escuché. Cuando guste, vuelva a llamarme.") { esperarPalabra() }
        }
    }

    // ---------- 3. Mandar al servidor y contestar ----------

    private fun procesar(dicho: String) {
        if (modo == Modo.CONFIRMAR_EMERGENCIA) {
            val palabras = dicho.lowercase().split(Regex("[\\s,.;:¡!¿?]+"))
            if ("no" in palabras && "sí" !in palabras && "si" !in palabras && !FRASES_EMERGENCIA.containsMatchIn(dicho)) {
                modo = Modo.NORMAL
                decir("Qué bueno. Aquí estoy si me necesita.") { esperarPalabra() }
            } else {
                enviarEmergencia("Pidió ayuda: \"$dicho\"")
            }
            return
        }

        fase = Fase.PENSANDO
        val miTurno = ++turno
        mostrar("Usted dijo: \"$dicho\"\nPensando…")
        if (FRASES_EMERGENCIA.containsMatchIn(dicho)) {
            // Aviso directo, sin esperar a Claude. El servidor evita mandar el aviso doble.
            red.execute {
                val enviados = try { servidor.emergencia("Dijo: \"$dicho\"") } catch (e: Exception) { 0 }
                if (enviados <= 0) principal.post { smsEmergencia("Dijo: \"$dicho\"") }
            }
        }
        red.execute {
            try {
                val r = servidor.hablar(dicho)
                principal.post { if (turno == miTurno) contestar(r) }
            } catch (e: Exception) {
                Log.e(TAG, "No se pudo hablar con el servidor", e)
                principal.post {
                    if (turno != miTurno) return@post
                    decir("Perdóneme, ahorita no me puedo conectar a internet. Inténtelo en un ratito.") {
                        esperarPalabra()
                    }
                }
            }
        }
    }

    private fun contestar(r: Servidor.Respuesta) {
        decir(r.texto) {
            var llamada: String? = null
            for (accion in r.acciones) {
                when (accion.optString("tipo")) {
                    "llamar" -> llamada = accion.optString("telefono")
                    "emergencia" -> if (accion.optInt("enviados", 0) <= 0) smsEmergencia(accion.optString("motivo"))
                }
            }
            if (!llamada.isNullOrBlank()) {
                llamar(llamada)
                esperarPalabra()
            } else {
                escuchar(seguimiento = true)
            }
        }
    }

    // ---------- Emergencias ----------

    private fun confirmarEmergencia() {
        detenerVosk()
        modo = Modo.CONFIRMAR_EMERGENCIA
        fase = Fase.HABLANDO
        decir("¿Está bien? Si necesita ayuda, diga sí. Si está bien, diga no.") { escuchar(seguimiento = false) }
    }

    private fun enviarEmergencia(motivo: String) {
        detenerTodoLoQueEscucha()
        // Lo de la emergencia va primero: se calla y se olvida de lo que estaba haciendo.
        turno++
        tts?.stop()
        alTerminarDeHablar.clear()
        modo = Modo.NORMAL
        fase = Fase.PENSANDO
        mostrar("Avisando a la familia…")
        red.execute {
            val enviados = try { servidor.emergencia(motivo) } catch (e: Exception) { 0 }
            principal.post {
                val sms = if (enviados <= 0) smsEmergencia(motivo) else false
                val frase = if (enviados > 0 || sms) {
                    "Ya avisé a su familia. Tranquilo, ahorita le llaman o vienen a verlo."
                } else {
                    "No pude avisar a su familia porque no hay internet. Si es grave, marque al nueve uno uno."
                }
                decir(frase) { esperarPalabra() }
            }
        }
    }

    /** Respaldo sin internet. Regresa true si se pudo mandar. */
    private fun smsEmergencia(motivo: String): Boolean {
        val telefono = ajustes.telefonoEmergencia
        if (telefono.isBlank() ||
            checkSelfPermission(Manifest.permission.SEND_SMS) != PackageManager.PERMISSION_GRANTED
        ) return false
        return try {
            val sms = if (Build.VERSION.SDK_INT >= 31) {
                getSystemService(SmsManager::class.java)
            } else {
                @Suppress("DEPRECATION") SmsManager.getDefault()
            }
            val texto = "EMERGENCIA (mensaje automático del asistente): $motivo. Llámenle o vayan a verlo."
            sms.sendMultipartTextMessage(telefono, null, sms.divideMessage(texto), null, null)
            true
        } catch (e: Exception) {
            Log.e(TAG, "No se pudo mandar el SMS", e)
            false
        }
    }

    private fun llamar(telefono: String) {
        if (checkSelfPermission(Manifest.permission.CALL_PHONE) != PackageManager.PERMISSION_GRANTED) return
        try {
            startActivity(
                Intent(Intent.ACTION_CALL, Uri.parse("tel:$telefono")).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
        } catch (e: Exception) {
            Log.e(TAG, "No se pudo llamar", e)
        }
    }

    // ---------- Recordatorios que manda el servidor ----------

    private val revisarMensajes: Runnable = object : Runnable {
        override fun run() {
            red.execute {
                val mensajes = try { servidor.mensajes() } catch (e: Exception) { emptyList() }
                if (mensajes.isNotEmpty()) principal.post { decirMensajes(mensajes) }
            }
            principal.postDelayed(this, CADA_CUANDO_MENSAJES)
        }
    }

    private fun decirMensajes(mensajes: List<Servidor.Mensaje>) {
        // Solo si está desocupado; si no, se dirán en la siguiente revisión.
        if (fase != Fase.ESPERANDO) return
        detenerVosk()
        fase = Fase.HABLANDO
        tono()
        fun siguiente(i: Int) {
            if (i == mensajes.size) {
                escuchar(seguimiento = true)  // para que pueda contestar "ya me la tomé"
                return
            }
            decir(mensajes[i].texto) {
                red.execute { try { servidor.entregado(mensajes[i].id) } catch (e: Exception) {} }
                siguiente(i + 1)
            }
        }
        siguiente(0)
    }

    // ---------- Voz y avisos en pantalla ----------

    private fun decir(texto: String, despues: () -> Unit) {
        fase = Fase.HABLANDO
        mostrar(texto)
        val t = tts
        val id = UUID.randomUUID().toString()
        alTerminarDeHablar[id] = despues
        val params = Bundle().apply { putInt(TextToSpeech.Engine.KEY_PARAM_STREAM, AudioManager.STREAM_MUSIC) }
        if (t == null || t.speak(texto, TextToSpeech.QUEUE_ADD, params, id) != TextToSpeech.SUCCESS) {
            alTerminarDeHablar.remove(id)
            despues()
        }
    }

    private fun terminoDeHablar(id: String) {
        principal.post { alTerminarDeHablar.remove(id)?.invoke() }
    }

    private fun tono() {
        tonos?.startTone(ToneGenerator.TONE_PROP_BEEP, 150)
    }

    private fun mostrar(texto: String) {
        estadoActual = texto
        principal.post { oyente?.invoke(texto) }
        if (iniciado) {
            getSystemService(NotificationManager::class.java).notify(ID_NOTIFICACION, notificacion(texto))
        }
    }

    private fun notificacion(texto: String): Notification {
        val gestor = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= 26) {
            gestor.createNotificationChannel(
                NotificationChannel(CANAL, "Asistente escuchando", NotificationManager.IMPORTANCE_LOW)
            )
        }
        val abrir = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE
        )
        return NotificationCompat.Builder(this, CANAL)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle("El asistente está escuchando")
            .setContentText(texto)
            .setOngoing(true)
            .setContentIntent(abrir)
            .build()
    }
}
