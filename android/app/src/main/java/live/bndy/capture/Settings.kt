package live.bndy.capture

import android.content.Context

class Settings(context: Context) {
    private val prefs = context.getSharedPreferences("bndy-capture", Context.MODE_PRIVATE)

    var apiUrl: String
        get() = prefs.getString("api_url", "") ?: ""
        set(value) = prefs.edit().putString("api_url", value.trim().trimEnd('/')).apply()

    var apiToken: String
        get() = prefs.getString("api_token", "") ?: ""
        set(value) = prefs.edit().putString("api_token", value.trim()).apply()
}
