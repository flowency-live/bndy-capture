package live.bndy.capture

import android.content.Context
import java.util.concurrent.Executors

object Delivery {
    private val executor = Executors.newSingleThreadExecutor()

    fun sendOne(context: Context, capture: Capture, complete: (Boolean, String?) -> Unit = { _, _ -> }) {
        executor.execute {
            val appContext = context.applicationContext
            val db = CaptureDb(appContext)
            try {
                val remoteId = CaptureApi(Settings(appContext), appContext).send(capture)
                db.markSent(capture.localId, remoteId)
                complete(true, null)
            } catch (error: Exception) {
                val message = error.message ?: error.javaClass.simpleName
                db.markFailed(capture.localId, message)
                complete(false, message)
            } finally {
                db.close()
            }
        }
    }

    fun retryAll(context: Context, complete: () -> Unit) {
        executor.execute {
            val appContext = context.applicationContext
            val db = CaptureDb(appContext)
            val api = CaptureApi(Settings(appContext), appContext)
            db.pending().forEach { capture ->
                try { db.markSent(capture.localId, api.send(capture)) }
                catch (error: Exception) { db.markFailed(capture.localId, error.message ?: error.javaClass.simpleName) }
            }
            db.close()
            complete()
        }
    }
}
