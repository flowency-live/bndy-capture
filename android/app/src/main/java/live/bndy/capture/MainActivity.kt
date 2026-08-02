package live.bndy.capture

import android.os.Bundle
import android.text.InputType
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import org.json.JSONArray

class MainActivity : AppCompatActivity() {
    private val prefs by lazy { getSharedPreferences("bndy-capture", MODE_PRIVATE) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val pad = (16 * resources.displayMetrics.density).toInt()
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }
        root.addView(TextView(this).apply { text = "Send to bndy"; textSize = 28f })
        root.addView(TextView(this).apply { text = "Share links, text or images from Android into a backlog for later processing." })

        val apiUrl = EditText(this).apply {
            hint = "API URL"
            setText(prefs.getString("api_url", ""))
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
        }
        val token = EditText(this).apply {
            hint = "Bearer token"
            setText(prefs.getString("api_token", ""))
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        val count = TextView(this)
        fun refresh() {
            val queue = JSONArray(prefs.getString("queue", "[]") ?: "[]")
            count.text = "Local backlog: ${queue.length()} item(s)"
        }

        root.addView(apiUrl)
        root.addView(token)
        root.addView(Button(this).apply {
            text = "Save settings"
            setOnClickListener {
                prefs.edit()
                    .putString("api_url", apiUrl.text.toString().trim().trimEnd('/'))
                    .putString("api_token", token.text.toString().trim())
                    .apply()
                Toast.makeText(this@MainActivity, "Settings saved", Toast.LENGTH_SHORT).show()
            }
        })
        root.addView(count)
        refresh()
        setContentView(root)
    }
}
