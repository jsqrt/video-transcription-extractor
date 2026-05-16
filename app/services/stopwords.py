"""Stop-word lists used by chaptering and summarization.

Kept as a Python module (not a resource file) so the package remains a
single ``pip install`` deliverable with no extra data files to ship.
"""

from __future__ import annotations

UKRAINIAN: frozenset[str] = frozenset({
    "а", "або", "але", "бо", "будь", "буде", "будемо", "будуть", "був", "була",
    "були", "було", "бути", "в", "вам", "вас", "ваш", "вашу", "ввесь", "весь",
    "ви", "від", "він", "вона", "вони", "воно", "все", "всі", "всього", "всіх",
    "давайте", "де", "до", "другий", "його", "її", "є", "ж", "за", "з", "зі",
    "з-за", "і", "їй", "їх", "й", "йому", "коли", "котра", "котрий", "кого",
    "кому", "кожен", "ми", "мене", "мені", "мій", "мого", "моя", "на", "над",
    "навіть", "нам", "нас", "наш", "наша", "наше", "не", "нема", "немає", "ні",
    "ніж", "ну", "о", "об", "один", "однак", "от", "по", "поки", "потім",
    "про", "просто", "та", "так", "також", "тебе", "тобі", "той", "тому",
    "тільки", "тут", "ти", "те", "теж", "тепер", "ти", "то", "треба", "тут",
    "у", "уже", "хоч", "хоча", "хто", "це", "цей", "ця", "ці", "чи", "чого",
    "чому", "чим", "що", "щоб", "щось", "як", "яка", "який", "які", "якщо",
    "ще", "саме", "сам", "своя", "свій", "себе",
    # discourse fillers frequently appear in Ukrainian speech
    "ну", "ось", "отже", "отож", "мовляв", "ніби", "типу", "значить",
})

ENGLISH: frozenset[str] = frozenset({
    "a", "about", "above", "after", "again", "against", "all", "am", "an",
    "and", "any", "are", "as", "at", "be", "because", "been", "before",
    "being", "below", "between", "both", "but", "by", "could", "did", "do",
    "does", "doing", "down", "during", "each", "few", "for", "from", "further",
    "had", "has", "have", "having", "he", "her", "here", "hers", "herself",
    "him", "himself", "his", "how", "i", "if", "in", "into", "is", "it",
    "its", "itself", "just", "me", "might", "more", "most", "my", "myself",
    "no", "nor", "not", "now", "of", "off", "on", "once", "only", "or",
    "other", "our", "ours", "ourselves", "out", "over", "own", "same",
    "she", "should", "so", "some", "such", "than", "that", "the", "their",
    "theirs", "them", "themselves", "then", "there", "these", "they", "this",
    "those", "through", "to", "too", "under", "until", "up", "very", "was",
    "we", "were", "what", "when", "where", "which", "while", "who", "whom",
    "why", "will", "with", "would", "you", "your", "yours", "yourself",
    "yourselves",
    # Common English contractions — without this list tokens like "here's"
    # escape the stopword filter and contaminate chapter titles with words
    # like "Here's" / "It's" / "We're".
    "ain't", "aren't", "can't", "couldn't", "didn't", "doesn't", "don't",
    "hadn't", "hasn't", "haven't", "he'd", "he'll", "he's", "here's",
    "how's", "i'd", "i'll", "i'm", "i've", "isn't", "it'd", "it'll", "it's",
    "let's", "shan't", "she'd", "she'll", "she's", "shouldn't", "that's",
    "there's", "they'd", "they'll", "they're", "they've", "wasn't", "we'd",
    "we'll", "we're", "we've", "weren't", "what's", "when's", "where's",
    "who's", "won't", "wouldn't", "you'd", "you'll", "you're", "you've",
    # Speech-filler discourse markers that clog tf-idf titles.
    "basically", "essentially", "literally", "actually", "obviously",
    "like", "really", "okay", "ok", "right", "well", "maybe",
})


ALL_STOPWORDS: frozenset[str] = UKRAINIAN | ENGLISH
