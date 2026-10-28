-- Bind :run_id to the exact full experiment ID in statistics.json.
-- All demographic analyses include both successful and failed workload requests.
SELECT CASE WHEN status_code=200 THEN 'successful' ELSE 'unsuccessful' END AS outcome, COUNT(*) AS requests
FROM requests WHERE run_id=:run_id AND traffic_category='workload' GROUP BY outcome;

SELECT is_banned, COUNT(*) AS requests FROM requests
WHERE run_id=:run_id AND traffic_category='workload' GROUP BY is_banned;

SELECT gender, COUNT(*) AS requests FROM requests
WHERE run_id=:run_id AND traffic_category='workload' GROUP BY gender;

-- DENSE_RANK is not used for the fifth-country boundary: include ties at the
-- fifth country by ROW_NUMBER's fifth count, as implemented in analyze.py.
SELECT country, COUNT(*) AS requests FROM requests
WHERE run_id=:run_id AND traffic_category='workload' GROUP BY country ORDER BY requests DESC, country;

SELECT age, COUNT(*) AS requests FROM requests
WHERE run_id=:run_id AND traffic_category='workload' GROUP BY age ORDER BY requests DESC, age;

SELECT income, COUNT(*) AS requests FROM requests
WHERE run_id=:run_id AND traffic_category='workload' GROUP BY income ORDER BY requests DESC, income;
