package mx.asistente.voz

import android.content.Context
import java.io.BufferedInputStream
import java.io.File
import java.io.FilterInputStream
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.util.zip.ZipInputStream

/** Descarga (una sola vez, ~40 MB) el modelo de Vosk en español que detecta la palabra de activación. */
object ModeloVoz {
    private const val URL_MODELO = "https://alphacephei.com/vosk/models/vosk-model-small-es-0.42.zip"

    fun carpeta(context: Context) = File(context.filesDir, "modelo-es")

    fun instalado(context: Context) = File(carpeta(context), ".listo").exists()

    /** Bloquea; llámalo fuera del hilo principal. `progreso` recibe de 0 a 100. */
    fun descargar(context: Context, progreso: (Int) -> Unit) {
        val destino = carpeta(context)
        destino.deleteRecursively()
        destino.mkdirs()
        val raiz = destino.canonicalPath + File.separator

        val con = URL(URL_MODELO).openConnection() as HttpURLConnection
        con.connectTimeout = 20_000
        con.readTimeout = 60_000
        try {
            if (con.responseCode != 200) throw IOException("No se pudo descargar el modelo (${con.responseCode})")
            val total = con.contentLengthLong
            var leidos = 0L
            var ultimo = -1
            val entrada = object : FilterInputStream(BufferedInputStream(con.inputStream)) {
                override fun read(b: ByteArray, off: Int, len: Int): Int {
                    val n = super.read(b, off, len)
                    if (n > 0 && total > 0) {
                        leidos += n
                        val pct = (leidos * 100 / total).toInt()
                        if (pct != ultimo) {
                            ultimo = pct
                            progreso(pct)
                        }
                    }
                    return n
                }
            }
            ZipInputStream(entrada).use { zip ->
                while (true) {
                    val item = zip.nextEntry ?: break
                    // El zip trae una carpeta raíz (vosk-model-small-es-0.42/...); se la quitamos.
                    val relativa = item.name.substringAfter('/', "")
                    if (relativa.isEmpty()) continue
                    val archivo = File(destino, relativa)
                    if (!archivo.canonicalPath.startsWith(raiz)) throw IOException("Ruta inválida en el zip")
                    if (item.isDirectory) {
                        archivo.mkdirs()
                    } else {
                        archivo.parentFile?.mkdirs()
                        archivo.outputStream().use { zip.copyTo(it) }
                    }
                }
            }
        } finally {
            con.disconnect()
        }
        File(destino, ".listo").createNewFile()
    }
}
