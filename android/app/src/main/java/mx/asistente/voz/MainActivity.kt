package mx.asistente.voz

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.SeekBar
import android.widget.TextView

/** Pantalla sencilla: estado grande, botón HABLAR, botón AYUDA y la configuración escondida. */
class MainActivity : Activity() {

    private lateinit var ajustes: Ajustes
    private lateinit var estado: TextView
    private lateinit var info: TextView
    private var descargando = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        ajustes = Ajustes(this)
        estado = findViewById(R.id.estado)
        info = findViewById(R.id.info)

        val servidor = findViewById<EditText>(R.id.servidor).apply { setText(ajustes.servidor) }
        val token = findViewById<EditText>(R.id.token).apply { setText(ajustes.token) }
        val palabra = findViewById<EditText>(R.id.palabra).apply { setText(ajustes.palabra) }
        val telefono = findViewById<EditText>(R.id.telefono).apply { setText(ajustes.telefonoEmergencia) }
        // La barra va de 0.5 (0) a 1.0 (10).
        val velocidad = findViewById<SeekBar>(R.id.velocidad).apply {
            progress = ((ajustes.velocidadVoz - 0.5f) * 20).toInt().coerceIn(0, 10)
        }
        val config = findViewById<LinearLayout>(R.id.config)

        findViewById<Button>(R.id.hablar).setOnClickListener {
            AsistenteService.iniciar(this, AsistenteService.ACCION_HABLAR)
        }
        findViewById<Button>(R.id.ayuda).setOnClickListener {
            AsistenteService.iniciar(this, AsistenteService.ACCION_AYUDA)
        }
        findViewById<Button>(R.id.mostrar_config).setOnClickListener {
            config.visibility = if (config.visibility == View.VISIBLE) View.GONE else View.VISIBLE
        }
        findViewById<Button>(R.id.guardar).setOnClickListener {
            ajustes.servidor = servidor.text.toString()
            ajustes.token = token.text.toString()
            ajustes.palabra = palabra.text.toString().ifBlank { "oye asistente" }
            ajustes.telefonoEmergencia = telefono.text.toString()
            ajustes.velocidadVoz = 0.5f + velocidad.progress / 20f
            // Reinicia el servicio para que tome los cambios.
            stopService(Intent(this, AsistenteService::class.java))
            pedirPermisosYArrancar()
        }
        findViewById<Button>(R.id.permisos_extra).setOnClickListener { permisosExtra() }
        findViewById<Button>(R.id.detener).setOnClickListener {
            stopService(Intent(this, AsistenteService::class.java))
        }

        if (!ajustes.completos) {
            config.visibility = View.VISIBLE
            estado.text = "Falta configurar el asistente."
        } else {
            pedirPermisosYArrancar()
        }
    }

    override fun onResume() {
        super.onResume()
        estado.text = AsistenteService.estadoActual
        AsistenteService.oyente = { estado.text = it }
    }

    override fun onPause() {
        AsistenteService.oyente = null
        super.onPause()
    }

    private fun pedirPermisosYArrancar() {
        val permisos = mutableListOf(
            Manifest.permission.RECORD_AUDIO,
            Manifest.permission.CALL_PHONE,
            Manifest.permission.SEND_SMS,
        )
        if (Build.VERSION.SDK_INT >= 33) permisos += Manifest.permission.POST_NOTIFICATIONS
        val faltan = permisos.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (faltan.isEmpty()) arrancar() else requestPermissions(faltan.toTypedArray(), 1)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            arrancar()
        } else {
            estado.text = "Sin permiso de micrófono no puedo escuchar."
        }
    }

    private fun arrancar() {
        if (!ajustes.completos) return
        if (ModeloVoz.instalado(this)) {
            AsistenteService.iniciar(this)
            return
        }
        if (descargando) return
        descargando = true
        estado.text = "Descargando el modelo de voz (una sola vez)…"
        Thread {
            try {
                ModeloVoz.descargar(this) { pct -> runOnUiThread { info.text = "Descargando modelo de voz: $pct%" } }
                runOnUiThread {
                    info.text = "Modelo de voz listo."
                    descargando = false
                    AsistenteService.iniciar(this)
                }
            } catch (e: Exception) {
                runOnUiThread {
                    descargando = false
                    estado.text = "No se pudo descargar el modelo de voz. Revise el internet y toque Guardar."
                    info.text = e.message
                }
            }
        }.start()
    }

    /** Para que Android no lo duerma y para poder llamar con la pantalla apagada. */
    private fun permisosExtra() {
        val energia = getSystemService(PowerManager::class.java)
        if (!energia.isIgnoringBatteryOptimizations(packageName)) {
            startActivity(
                Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName"))
            )
        } else if (!Settings.canDrawOverlays(this)) {
            startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
        } else {
            info.text = "Listo: batería sin restricciones y permiso para mostrarse encima. " +
                "En Xiaomi/Huawei/Oppo/Samsung revise también el 'inicio automático' (ver README)."
        }
    }
}
