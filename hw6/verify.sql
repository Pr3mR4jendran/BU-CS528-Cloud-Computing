WITH reconstructed AS (
    SELECT r.*,
           COALESCE(o.country, CASE WHEN r.ip_country_observed THEN ip.country END) AS country,
           COALESCE(c.is_banned, 0) AS is_banned,
           TIME(r.client_time) AS time_of_day
    FROM hw6.requests AS r
    LEFT JOIN hw6.client_ips AS ip USING (client_ip)
    LEFT JOIN hw6.request_country_observations AS o USING (request_id)
    LEFT JOIN hw6.countries AS c
      ON c.country = COALESCE(o.country, CASE WHEN r.ip_country_observed THEN ip.country END)
)
SELECT COUNT(*)
FROM hw5.requests AS s
LEFT JOIN reconstructed AS r ON r.request_id = s.request_id COLLATE utf8mb4_0900_bin
WHERE r.request_id IS NULL OR NOT (
    s.request_time <=> r.request_time
    AND CAST(s.client_ip AS BINARY) <=> CAST(r.client_ip AS BINARY)
    AND CAST(s.country AS BINARY) <=> CAST(r.country AS BINARY)
    AND CAST(s.gender AS BINARY) <=> CAST(r.gender AS BINARY)
    AND CAST(s.age AS BINARY) <=> CAST(r.age AS BINARY)
    AND CAST(s.income AS BINARY) <=> CAST(r.income AS BINARY)
    AND s.is_banned <=> r.is_banned
    AND s.time_of_day <=> r.time_of_day
    AND s.client_time <=> r.client_time
    AND CAST(s.requested_file AS BINARY) <=> CAST(r.requested_file AS BINARY)
    AND s.status_code <=> r.status_code
    AND CAST(s.peer_ip AS BINARY) <=> CAST(r.peer_ip AS BINARY)
    AND CAST(s.method AS BINARY) <=> CAST(r.method AS BINARY)
    AND CAST(s.run_id AS BINARY) <=> CAST(r.run_id AS BINARY)
    AND CAST(s.traffic_category AS BINARY) <=> CAST(r.traffic_category AS BINARY)
);
