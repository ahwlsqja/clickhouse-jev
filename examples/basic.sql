-- 합성 행으로 각 SQL 함수를 호출한다. 설정한 endpoint에 모델 요청을 보낸다.
SELECT jev_prob(
    toJSONString(map('item', 'red apple')),
    'Is the item red?'
) AS probability;

SELECT jev_choice(
    toJSONString(map('item', 'yellow banana')),
    'Choose the item color.',
    toJSONString(['red', 'yellow', 'green'])
) AS color;

SELECT jev_score_norm(
    toJSONString(map('description', 'a small budget hotel')),
    'Rate the level of luxury.',
    toJSONString(['budget', 'mid-range', 'premium', 'luxury'])
) AS luxury;
