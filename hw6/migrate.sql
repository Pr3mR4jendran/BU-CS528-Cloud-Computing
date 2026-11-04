INSERT INTO hw6.countries (country, is_banned)
SELECT country COLLATE utf8mb4_0900_bin, MAX(is_banned)
FROM hw5.requests
WHERE country IS NOT NULL
GROUP BY country COLLATE utf8mb4_0900_bin;

INSERT INTO hw6.client_ips (client_ip, country)
SELECT client_ip COLLATE utf8mb4_0900_bin,
       CASE WHEN COUNT(DISTINCT country COLLATE utf8mb4_0900_bin) = 1
            THEN MAX(country COLLATE utf8mb4_0900_bin) END
FROM hw5.requests
WHERE client_ip IS NOT NULL
GROUP BY client_ip COLLATE utf8mb4_0900_bin;

INSERT INTO hw6.requests (
    request_id, request_time, client_ip, ip_country_observed, gender, age,
    income, client_time, requested_file, status_code, peer_ip, method, run_id, traffic_category
)
SELECT r.request_id, r.request_time, r.client_ip, r.country IS NOT NULL AND ip.country IS NOT NULL,
       r.gender, r.age, r.income, r.client_time, r.requested_file, r.status_code,
       r.peer_ip, r.method, r.run_id, r.traffic_category
FROM hw5.requests AS r
LEFT JOIN hw6.client_ips AS ip ON ip.client_ip = r.client_ip COLLATE utf8mb4_0900_bin;

INSERT INTO hw6.request_country_observations (request_id, country)
SELECT r.request_id, r.country
FROM hw5.requests AS r
LEFT JOIN hw6.client_ips AS ip ON ip.client_ip = r.client_ip COLLATE utf8mb4_0900_bin
WHERE r.country IS NOT NULL AND ip.country IS NULL;
