package mx.asistente.voz

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat

/**
 * Al prender el celular, Android no deja abrir el micrófono desde segundo plano.
 * Mostramos una notificación: al tocarla se abre la app y el asistente arranca solo.
 */
class ArranqueReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        val gestor = context.getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= 26) {
            gestor.createNotificationChannel(
                NotificationChannel("arranque", "Activar asistente", NotificationManager.IMPORTANCE_HIGH)
            )
        }
        val abrir = PendingIntent.getActivity(
            context, 0, Intent(context, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE
        )
        gestor.notify(
            2,
            NotificationCompat.Builder(context, "arranque")
                .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                .setContentTitle("Toque aquí para activar el asistente")
                .setContentText("El celular se reinició y el asistente está apagado.")
                .setContentIntent(abrir)
                .setAutoCancel(true)
                .build(),
        )
    }
}
