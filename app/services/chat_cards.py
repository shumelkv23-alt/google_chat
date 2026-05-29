def build_chart_card(title: str, image_url: str, summary: str = "") -> dict:
    """Card с заголовком, картинкой графика и опциональным текстовым подытогом."""
    widgets: list[dict] = [
        {"image": {"imageUrl": image_url, "altText": title or "график"}}
    ]
    if summary:
        widgets.append({"textParagraph": {"text": summary}})

    card: dict = {
        "card": {
            "header": {"title": title} if title else {},
            "sections": [{"widgets": widgets}],
        },
        "cardId": "vacancy-chart",
    }
    return {"cardsV2": [card]}
