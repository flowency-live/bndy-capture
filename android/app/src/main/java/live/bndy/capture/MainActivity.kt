package live.bndy.capture

import android.os.Bundle
import android.text.InputType
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity

class MainActivity : AppCompatActivity() {
    private lateinit var settings: Settings
    private lateinit var listContainer: LinearLayout

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        settings = Settings(this)
        render()
    }

    override fun onResume() {
        super.onResume()
        if (::listContainer.isInitialized) refreshList()
    }

    private fun render() {
        val pad = (16 * resources.displayMetrics.density).toInt()
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }
        root.addView(TextView(this).apply { text = "Send to bndy"; textSize = 28f })
        root.addView(TextView(this).apply { text = "Share links, text or images from Android. Captures remain local until successfully delivered." })

        val apiUrl = EditText(this).apply {
            hint = "API URL"
            setText(settings.apiUrl)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
        }
        val token = EditText(this).apply {
            hint = "Bearer token"
            setText(settings.apiToken)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        root.addView(apiUrl)
        root.addView(token)
        root.addView(Button(this).apply {
            text = "Save settings"
            setOnClickListener {
                settings.apiUrl = apiUrl.text.toString()
                settings.apiToken = token.text.toString()
                Toast.makeText(this@MainActivity, "Settings saved", Toast.LENGTH_SHORT).show()
            }
        })
        root.addView(Button(this).apply {
            text = "Retry unsent captures"
            setOnClickListener {
                isEnabled = false
                Delivery.retryAll(this@MainActivity) {
                    runOnUiThread {
                        isEnabled = true
                        refreshList()
                        Toast.makeText(this@MainActivity, "Retry complete", Toast.LENGTH_SHORT).show()
                    }
                }
            }
        })
        root.addView(TextView(this).apply { text = "Recent captures"; textSize = 20f })
        listContainer = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val scroll = ScrollView(this).apply { addView(listContainer) }
        root.addView(scroll, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            0,
            1f
        ))
        setContentView(root)
        refreshList()
    }

    private fun refreshList() {
        val db = CaptureDb(this)
        val captures = db.list(100)
        db.close()
        listContainer.removeAllViews()
        if (captures.isEmpty()) {
            listContainer.addView(TextView(this).apply { text = "No captures yet." })
            return
        }
        captures.forEach { capture ->
            listContainer.addView(TextView(this).apply {
                val title = capture.sharedUrl ?: capture.sharedText ?: capture.imageUri ?: "Shared item"
                val error = capture.lastError?.let { "\nError: $it" }.orEmpty()
                text = "${capture.deliveryStatus.uppercase()} · ${capture.capturedAt}\n${title.take(300)}$error"
                setPadding(0, 12, 0, 12)
            })
        }
    }
}
