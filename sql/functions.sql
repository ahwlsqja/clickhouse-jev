-- 실행 UDF에 명시적으로 넘긴 JSON 행만 평가한다.
CREATE FUNCTION IF NOT EXISTS jev_prob AS (row_json, question) ->
    JSONExtractFloat(jev_eval(row_json, question, 'noul', '[]'), 'noul');
CREATE FUNCTION IF NOT EXISTS jev AS (row_json, question, threshold) ->
    jev_prob(row_json, question) >= threshold;
CREATE FUNCTION IF NOT EXISTS jev_choice AS (row_json, question, options_json) ->
    JSONExtractString(jev_eval(row_json, question, 'choice', options_json), 'choice');
CREATE FUNCTION IF NOT EXISTS jev_score AS (row_json, question, options_json) ->
    JSONExtractFloat(jev_eval(row_json, question, 'score', options_json), 'score');
CREATE FUNCTION IF NOT EXISTS jev_score_norm AS (row_json, question, options_json) ->
    jev_score(row_json, question, options_json) / (JSONLength(options_json) - 1);
