package mx.asistente.voz

import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/** Cliente del servidor del asistente. Todas las llamadas bloquean: úsalas fuera del hilo principal. */
class Servidor(private val ajustes: Ajustes) {

    data class Respuesta(val texto: String, val acciones: List<JSONObject>)
    data class Mensaje(val id: Int, val texto: String)

    fun hablar(texto: String): Respuesta {
        val json = JSONObject(peticion("POST", "/api/hablar", JSONObject().put("texto", texto), 90_000))
        val acciones = json.optJSONArray("acciones") ?: JSONArray()
        return Respuesta(
            json.getString("texto"),
            (0 until acciones.length()).map { acciones.getJSONObject(it) },
        )
    }

    fun mensajes(): List<Mensaje> {
        val lista = JSONArray(peticion("GET", "/api/mensajes"))
        return (0 until lista.length()).map {
            val m = lista.getJSONObject(it)
            Mensaje(m.getInt("id"), m.getString("texto"))
        }
    }

    fun entregado(id: Int) {
        peticion("POST", "/api/mensajes/$id/entregado")
    }

    /** Regresa a cuántos familiares les llegó el aviso. */
    fun emergencia(motivo: String): Int =
        JSONObject(peticion("POST", "/api/emergencia", JSONObject().put("motivo", motivo))).getInt("enviados")

    private fun peticion(metodo: String, ruta: String, cuerpo: JSONObject? = null, espera: Int = 20_000): String {
        val con = URL(ajustes.servidor + ruta).openConnection() as HttpURLConnection
        try {
            con.requestMethod = metodo
            con.connectTimeout = 10_000
            con.readTimeout = espera
            con.setRequestProperty("Authorization", "Bearer ${ajustes.token}")
            if (cuerpo != null || metodo == "POST") {
                con.doOutput = true
                con.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                con.outputStream.use { it.write((cuerpo ?: JSONObject()).toString().toByteArray()) }
            }
            val codigo = con.responseCode
            if (codigo !in 200..299) throw IOException("El servidor respondió $codigo")
            return con.inputStream.bufferedReader().use { it.readText() }
        } finally {
            con.disconnect()
        }
    }
}
