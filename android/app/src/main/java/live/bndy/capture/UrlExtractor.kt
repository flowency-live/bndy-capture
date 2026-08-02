package live.bndy.capture

object UrlExtractor {
    private val pattern = Regex("https?://[^\\s<>\\\"']+", RegexOption.IGNORE_CASE)
    fun first(text: String?): String? = text?.let { pattern.find(it)?.value }?.trimEnd(')', ',', '.', ';', '!', '?')
}
