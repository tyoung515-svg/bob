import pytest
from core.nodes.execute import LOCALE_DIRECTIVE, locale_directive_message, _messages_to_prompt

# Byte-exactness of the directive texts
def test_locale_directive_bytes():
    assert LOCALE_DIRECTIVE["zh-Hans"] == "只用简体中文回答。无论用户消息或上下文使用何种语言，你的全部回复都必须是简体中文。"
    assert LOCALE_DIRECTIVE["zh-Hant"] == "只用繁體中文回答。無論使用者訊息或上下文使用何種語言，你的全部回覆都必須是繁體中文。"

# Returns correct dict for valid locales
def test_locale_directive_message_zh_Hans():
    result = locale_directive_message("zh-Hans")
    assert result == {"role": "system", "content": LOCALE_DIRECTIVE["zh-Hans"]}

def test_locale_directive_message_zh_Hant():
    result = locale_directive_message("zh-Hant")
    assert result == {"role": "system", "content": LOCALE_DIRECTIVE["zh-Hant"]}

# Returns None for non-injectable inputs
def test_locale_directive_message_none():
    assert locale_directive_message(None) is None

def test_locale_directive_message_empty_string():
    assert locale_directive_message("") is None

def test_locale_directive_message_en():
    assert locale_directive_message("en") is None

def test_locale_directive_message_unknown():
    assert locale_directive_message("fr") is None

def test_locale_directive_message_list():
    assert locale_directive_message(["zh-Hans"]) is None

def test_locale_directive_message_dict():
    assert locale_directive_message({"x": 1}) is None

def test_locale_directive_message_int():
    assert locale_directive_message(123) is None

# Front-most ordering after splices
def test_front_most_after_splices():
    face_system = {"role": "system", "content": "face"}
    user = {"role": "user", "content": "hello"}
    project_context = {"role": "system", "content": "project"}
    recall = {"role": "system", "content": "recall"}

    messages = [face_system, user]
    messages.insert(0, project_context)   # now [project, face, user]
    messages.insert(0, recall)            # now [recall, project, face, user]

    d = locale_directive_message("zh-Hans")
    messages.insert(0, d)                 # now [directive, recall, project, face, user]

    assert messages[0] == d
    assert messages[0]["content"] == LOCALE_DIRECTIVE["zh-Hans"]
    assert messages[1] is recall

# Ordering via _messages_to_prompt
def test_prompt_ordering():
    d = locale_directive_message("zh-Hans")
    recall = {"role": "system", "content": "recall"}
    project = {"role": "system", "content": "project"}
    user = {"role": "user", "content": "hi"}

    # Simulate the order after injection: directive, recall, project, user
    messages = [d, recall, project, user]
    prompt = _messages_to_prompt(messages)

    # Find indices of the directive and recall/project texts in the prompt
    idx_directive = prompt.find(LOCALE_DIRECTIVE["zh-Hans"])
    idx_recall = prompt.find("recall")
    idx_project = prompt.find("project")
    assert idx_directive != -1
    assert idx_recall != -1
    assert idx_project != -1
    assert idx_directive < idx_recall
    assert idx_directive < idx_project
