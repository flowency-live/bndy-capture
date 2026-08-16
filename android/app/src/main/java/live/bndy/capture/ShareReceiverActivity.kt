package live.bndy.capture

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import java.io.File
import java.time.Instant
import java.util.UUID

class ShareReceiverActivity : AppCompatActivity() {
    private var sharedText: String? = null
    private var imageUri: Uri? = null
    private var mimeType: String = "text/plain"

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        mimeType = intent.type ?: "text/plain"
        if (intent.action == Intent.ACTION_SEND) {
            sharedText = intent.getStringExtra(Intent.EXTRA_TEXT) ?: intent.getStringExtra(Intent.EXTRA_SUBJECT)
            imageUri = intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
        }
        render()
    }

    private fun render() {
        val pad = (20 * resources.displayMetrics.density).toInt()
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }
        layout.addView(TextView(this).apply { text = "Send to bndy"; textSize = 26f })
        layout.addView(TextView(this).apply { text = sharedText ?: imageUri?.toString() ?: "Shared item" })
        val note = EditText(this).apply { hint = "Optional note" }
        layout.addView(note)
        layout.addView(Button(this).apply {
            text = "Add to bndy backlog"
            setOnClickListener { save(note.text.toString()) }
        })
        layout.addView(Button(this).apply { text = "Cancel"; setOnClickListener { finish() } })
        setContentView(layout)
    }

    private fun persistSharedImage(uri: Uri?): String? {
        if (uri == null || !mimeType.startsWith("image/")) return uri?.toString()
        val extension = when (mimeType) {
            "image/jpeg" -> "jpg"
            "image/png" -> "png"
            "image/webp" -> "webp"
            "image/gif" -> "gif"
            else -> "img"
        }
        val dir = File(filesDir, "capture-images").apply { mkdirs() }
        val file = File(dir, "${UUID.randomUUID()}.$extension")
        contentResolver.openInputStream(uri)?.use { input ->
            file.outputStream().use { output -> input.copyTo(output) }
        } ?: throw IllegalStateException("Unable to read shared image")
        return file.absolutePath
    }

    private fun save(note: String) {
        try {
            val storedImage = persistSharedImage(imageUri)
            val db = CaptureDb(this)
            val id = db.insert(
                capturedAt = Instant.now().toString(),
                sharedText = sharedText,
                sharedUrl = UrlExtractor.first(sharedText),
                mimeType = mimeType,
                sourceApp = callingPackage ?: referrer?.host,
                imageUri = storedImage,
                note = note.trim().ifBlank { null }
            )
            val capture = db.get(id)
            db.close()

            Toast.makeText(this, "Added to bndy backlog", Toast.LENGTH_SHORT).show()
            val settings = Settings(this)
            if (capture != null && settings.apiUrl.isNotBlank() && settings.apiToken.isNotBlank()) {
                Delivery.sendOne(this, capture)
            }
            finish()
        } catch (error: Exception) {
            Toast.makeText(this, "Could not save shared image: ${error.message}", Toast.LENGTH_LONG).show()
        }
    }
}
