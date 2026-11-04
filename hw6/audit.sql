SELECT COUNT(*) AS source_requests,
       COUNT(CASE WHEN status_code <> 200 THEN 1 END) AS source_non_200,
       COUNT(CASE WHEN client_ip IS NULL AND country IS NOT NULL THEN 1 END) AS source_countries_without_ip,
       COUNT(CASE WHEN is_banned NOT IN (0, 1) OR (country IS NULL AND is_banned <> 0) THEN 1 END) AS invalid_ban_flags,
       COUNT(CASE WHEN NOT (time_of_day <=> TIME(client_time)) THEN 1 END) AS time_conflicts
FROM hw5.requests;

SELECT COUNT(*) AS source_ips,
       COUNT(CASE WHEN countries > 1 THEN 1 END) AS ip_conflicts,
       COUNT(CASE WHEN workload_countries > 1 THEN 1 END) AS workload_ip_conflicts,
       CAST(COALESCE(SUM(CASE WHEN countries > 1 THEN country_observations END), 0)
            AS UNSIGNED) AS source_countries_for_ambiguous_ips,
       COUNT(CASE WHEN packed_ip IS NULL OR spellings > 1 THEN 1 END) AS ip_format_conflicts
FROM (
    SELECT INET6_ATON(client_ip) AS packed_ip,
           COUNT(DISTINCT country COLLATE utf8mb4_0900_bin) AS countries,
           COUNT(DISTINCT CASE WHEN run_id = :run_id AND traffic_category = 'workload'
                               THEN country COLLATE utf8mb4_0900_bin END) AS workload_countries,
           COUNT(country) AS country_observations,
           COUNT(DISTINCT client_ip COLLATE utf8mb4_0900_bin) AS spellings
    FROM hw5.requests
    WHERE client_ip IS NOT NULL
    GROUP BY packed_ip
) AS ip_groups;

SELECT COUNT(*) AS source_countries,
       COUNT(CASE WHEN min_flag <> max_flag THEN 1 END) AS ban_conflicts
FROM (
    SELECT MIN(is_banned) AS min_flag, MAX(is_banned) AS max_flag
    FROM hw5.requests
    WHERE country IS NOT NULL
    GROUP BY country COLLATE utf8mb4_0900_bin
) AS country_groups;

SELECT COUNT(*) AS source_failures,
       COUNT(CASE WHEN r.request_id IS NULL THEN 1 END) AS orphan_failures,
       COUNT(CASE WHEN r.status_code = 200 OR r.status_code <> f.error_code
                       OR r.request_time <> f.request_time
                       OR CAST(r.requested_file AS BINARY) <> CAST(f.requested_file AS BINARY)
                  THEN 1 END) AS failure_conflicts
FROM hw5.failed_requests AS f
LEFT JOIN hw5.requests AS r USING (request_id);
