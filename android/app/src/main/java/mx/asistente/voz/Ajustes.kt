package mx.asistente.voz

import android.content.Context

/** Configuración guardada en el celular. */
class Ajustes(context: Context) {
    private val prefs = context.getSharedPreferences("ajustes", Context.MODE_PRIVATE)

    var servidor: String
        get() = prefs.getString("servidor", "")!!
        set(v) = prefs.edit().putString("servidor", v.trim().trimEnd('/')).apply()

    var token: String
        get() = prefs.getString("token", "")!!
        set(v) = prefs.edit().putString("token", v.trim()).apply()

    /** Frase que despierta al asistente. Sus palabras deben existir en el modelo de Vosk en español. */
    var palabra: String
        get() = prefs.getString("palabra", "oye asistente")!!
        set(v) = prefs.edit().putString("palabra", v.trim().lowercase()).apply()

    /** Número al que se manda un SMS si no hay internet durante una emergencia. */
    var telefonoEmergencia: String
        get() = prefs.getString("telefono_emergencia", "")!!
        set(v) = prefs.edit().putString("telefono_emergencia", v.trim()).apply()

    /** 1.0 es la velocidad normal; más bajo habla más despacio. */
    var velocidadVoz: Float
        get() = prefs.getFloat("velocidad_voz", 0.85f)
        set(v) = prefs.edit().putFloat("velocidad_voz", v).apply()

    val completos: Boolean
        get() = servidor.isNotBlank() && token.isNotBlank() && palabra.isNotBlank()
}
