-- Stage 4B.1 review artifact: canonical sector-summary shadow materialization.
--
-- This file is intentionally NOT a production cutover script.  It creates only
-- fixed shadow objects, reads stdata.st_data and taxonomy metadata, and has no
-- RENAME, DROP, ALTER, or write targeting a production summary object.
--
-- Canonical reporting market context:
--   exchange IN ('A', 'N', 'Q', 'T')
--   CS: d.type = 'CS'
--   EQ: d.type IN ('CS', 'UN')
-- The internal exchange '*' is a direct aggregate across A/N/Q/T only.

CREATE TABLE stdata.st_sector_summary_shadow (
  weekdate date NOT NULL,
  exchange char(1) NOT NULL,
  type varchar(4) NOT NULL DEFAULT 'CS',
  sector_code varchar(8) NOT NULL,
  sector_name varchar(30),

  total int unsigned NOT NULL DEFAULT 0,

  newbulls int unsigned NOT NULL DEFAULT 0,
  bulls int unsigned NOT NULL DEFAULT 0,
  weakbulls int unsigned NOT NULL DEFAULT 0,
  newbears int unsigned NOT NULL DEFAULT 0,
  bears int unsigned NOT NULL DEFAULT 0,
  weakbears int unsigned NOT NULL DEFAULT 0,
  flats int unsigned NOT NULL DEFAULT 0,
  notrend int unsigned NOT NULL DEFAULT 0,

  bullish_count int unsigned NOT NULL DEFAULT 0,
  bearish_count int unsigned NOT NULL DEFAULT 0,
  neutral_count int unsigned NOT NULL DEFAULT 0,

  avg_trend_cnt decimal(8,2),
  avg_trend_cnt_bullish decimal(8,2),
  avg_trend_cnt_bearish decimal(8,2),
  max_trend_cnt smallint unsigned NOT NULL DEFAULT 0,

  avg_mt_cnt decimal(8,2),
  avg_mt_cnt_bullish decimal(8,2),
  avg_mt_cnt_bearish decimal(8,2),
  max_mt_cnt smallint unsigned NOT NULL DEFAULT 0,

  avg_rsi decimal(8,2),
  bull_avg_rsi decimal(8,2),

  rsi_ge_110_count int unsigned NOT NULL DEFAULT 0,
  rsi_ge_120_count int unsigned NOT NULL DEFAULT 0,

  young_bullish_count int unsigned NOT NULL DEFAULT 0,
  mature_bullish_count int unsigned NOT NULL DEFAULT 0,

  -- Intentionally nullable: a sector with no classified rows has no
  -- directional percentage or leadership score, rather than a false zero.
  bull_pct decimal(10,6) DEFAULT NULL,
  leadership_score decimal(12,4) DEFAULT NULL,

  PRIMARY KEY (weekdate, exchange, type, sector_code),
  KEY idx_exchange_week (exchange, weekdate),
  KEY idx_type_week_score (type, weekdate, leadership_score),
  KEY idx_exchange_type_week_score (exchange, type, weekdate, leadership_score)
)
ENGINE=MyISAM
DEFAULT CHARSET=latin1;

CREATE TABLE stdata.st_sector_summary_coverage_shadow (
  weekdate date NOT NULL,
  exchange char(1) NOT NULL,
  type varchar(4) NOT NULL,
  classified_population_count int unsigned NOT NULL DEFAULT 0,
  mapped_classified_count int unsigned NOT NULL DEFAULT 0,
  unmapped_classified_count int unsigned NOT NULL DEFAULT 0,
  PRIMARY KEY (weekdate, exchange, type),
  KEY idx_type_exchange_week (type, exchange, weekdate)
)
ENGINE=MyISAM
DEFAULT CHARSET=latin1;

DELIMITER $$

CREATE PROCEDURE stdata.UpdateSectorSummaryNewShadow(
  IN dWeekDate DATE,
  IN pType VARCHAR(4)
)
proc: BEGIN
  DECLARE vWeekDate DATE;
  DECLARE vType VARCHAR(4);

  IF dWeekDate IS NULL THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'dWeekDate is required';
  END IF;

  -- Advance to the next Friday, including the supplied date when it is Friday.
  SET vWeekDate = DATE_ADD(
    dWeekDate,
    INTERVAL MOD(6 - DAYOFWEEK(dWeekDate) + 7, 7) DAY
  );
  SET vType = UPPER(TRIM(COALESCE(pType, '')));
  IF vType = '' THEN
    SET vType = 'CS';
  END IF;
  IF vType NOT IN ('CS', 'EQ') THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'pType must be CS or EQ for shadow materialization';
  END IF;

  -- A duplicate identical taxonomy row is harmless after DISTINCT.  Conflicting
  -- mapped sector definitions are not: fail before deletion rather than multiply
  -- or arbitrarily assign a source security to multiple named sectors.
  IF EXISTS (
    SELECT 1
    FROM (
      SELECT s.industry_code
      FROM stdata.st_listsectorsandindustries AS s
      WHERE s.sector_code IS NOT NULL
      GROUP BY s.industry_code
      HAVING COUNT(DISTINCT CONCAT(
        s.sector_code, CHAR(31), COALESCE(s.sector_name, '')
      )) > 1
    ) AS conflicting_taxonomy
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'conflicting mapped taxonomy definitions by industry_code';
  END IF;

  -- The primary key groups by sector_code, not sector_name.  A code must have
  -- one normalized effective name so it cannot yield duplicate sector rows.
  IF EXISTS (
    SELECT 1
    FROM (
      SELECT s.sector_code
      FROM stdata.st_listsectorsandindustries AS s
      WHERE s.sector_code IS NOT NULL
      GROUP BY s.sector_code
      HAVING COUNT(DISTINCT COALESCE(s.sector_name, '')) > 1
    ) AS conflicting_sector_names
  ) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'conflicting effective sector names by sector_code';
  END IF;

  -- MyISAM has no transactional rollback.  These deletes and inserts are a
  -- deterministic rebuild for shadow validation only; interruption can leave
  -- the target week/type incomplete until this procedure is rerun.
  DELETE FROM stdata.st_sector_summary_shadow
  WHERE weekdate = vWeekDate AND type = vType;

  DELETE FROM stdata.st_sector_summary_coverage_shadow
  WHERE weekdate = vWeekDate AND type = vType;

  -- Cross joining scope produces one direct source aggregation for each source
  -- exchange and one independent '*' aggregation across exactly A/N/Q/T.  It
  -- never combines stored averages.
  INSERT INTO stdata.st_sector_summary_shadow (
    weekdate, exchange, type, sector_code, sector_name,
    total,
    newbulls, bulls, weakbulls, newbears, bears, weakbears, flats, notrend,
    bullish_count, bearish_count, neutral_count,
    avg_trend_cnt, avg_trend_cnt_bullish, avg_trend_cnt_bearish, max_trend_cnt,
    avg_mt_cnt, avg_mt_cnt_bullish, avg_mt_cnt_bearish, max_mt_cnt,
    avg_rsi, bull_avg_rsi,
    rsi_ge_110_count, rsi_ge_120_count,
    young_bullish_count, mature_bullish_count,
    bull_pct, leadership_score
  )
  SELECT
    a.weekdate,
    a.exchange,
    vType,
    a.sector_code,
    a.sector_name,
    a.total,
    a.newbulls, a.bulls, a.weakbulls,
    a.newbears, a.bears, a.weakbears, a.flats, a.notrend,
    a.bullish_count, a.bearish_count, a.neutral_count,
    a.avg_trend_cnt, a.avg_trend_cnt_bullish, a.avg_trend_cnt_bearish,
    a.max_trend_cnt,
    a.avg_mt_cnt, a.avg_mt_cnt_bullish, a.avg_mt_cnt_bearish, a.max_mt_cnt,
    a.avg_rsi, a.bull_avg_rsi,
    a.rsi_ge_110_count, a.rsi_ge_120_count,
    a.young_bullish_count, a.mature_bullish_count,
    a.bullish_count / NULLIF(a.bullish_count + a.bearish_count, 0) AS bull_pct,
    (
      a.avg_rsi
      * (a.bullish_count / NULLIF(a.bullish_count + a.bearish_count, 0))
      + a.avg_mt_cnt * 0.25
    ) AS leadership_score
  FROM (
    SELECT
      d.weekdate,
      CASE WHEN scope.is_all_exchanges = 1 THEN '*' ELSE d.exchange END AS exchange,
      s.sector_code,
      s.sector_name,

      COUNT(*) AS total,

      COALESCE(SUM(d.trend = 'v^'), 0) AS newbulls,
      COALESCE(SUM(d.trend = '^+'), 0) AS bulls,
      COALESCE(SUM(d.trend = '^-'), 0) AS weakbulls,
      COALESCE(SUM(d.trend = '^v'), 0) AS newbears,
      COALESCE(SUM(d.trend = 'v-'), 0) AS bears,
      COALESCE(SUM(d.trend = 'v+'), 0) AS weakbears,
      COALESCE(SUM(d.trend = '='), 0) AS flats,
      COALESCE(SUM(d.trend = '--'), 0) AS notrend,

      COALESCE(SUM(d.trend IN ('^+', '^-', 'v^')), 0) AS bullish_count,
      COALESCE(SUM(d.trend IN ('^v', 'v+', 'v-')), 0) AS bearish_count,
      COALESCE(SUM(d.trend IN ('--', '=')), 0) AS neutral_count,

      AVG(CASE
        WHEN d.trend IN ('^+', '^-', 'v^', '^v', 'v+', 'v-')
        THEN d.trend_cnt
      END) AS avg_trend_cnt,
      AVG(CASE WHEN d.trend IN ('^+', '^-', 'v^') THEN d.trend_cnt END)
        AS avg_trend_cnt_bullish,
      AVG(CASE WHEN d.trend IN ('^v', 'v+', 'v-') THEN d.trend_cnt END)
        AS avg_trend_cnt_bearish,
      COALESCE(MAX(d.trend_cnt), 0) AS max_trend_cnt,

      AVG(CASE
        WHEN d.trend IN ('^+', '^-', 'v^', '^v', 'v+', 'v-')
        THEN d.mt_cnt
      END) AS avg_mt_cnt,
      AVG(CASE WHEN d.trend IN ('^+', '^-', 'v^') THEN d.mt_cnt END)
        AS avg_mt_cnt_bullish,
      AVG(CASE WHEN d.trend IN ('^v', 'v+', 'v-') THEN d.mt_cnt END)
        AS avg_mt_cnt_bearish,
      COALESCE(MAX(d.mt_cnt), 0) AS max_mt_cnt,

      -- Persisted MySQL numeric RSI values are finite.  No lower bound applies.
      AVG(CASE
        WHEN d.trend IN ('^+', '^-', 'v^', '^v', 'v+', 'v-')
         AND d.rsi IS NOT NULL
         AND d.rsi <= 10000
        THEN d.rsi
      END) AS avg_rsi,
      AVG(CASE
        WHEN d.trend IN ('^+', '^-', 'v^')
         AND d.rsi IS NOT NULL
         AND d.rsi <= 10000
        THEN d.rsi
      END) AS bull_avg_rsi,

      SUM(CASE
        WHEN d.rsi IS NOT NULL AND d.rsi <= 10000 AND d.rsi >= 110
        THEN 1 ELSE 0
      END) AS rsi_ge_110_count,
      SUM(CASE
        WHEN d.rsi IS NOT NULL AND d.rsi <= 10000 AND d.rsi >= 120
        THEN 1 ELSE 0
      END) AS rsi_ge_120_count,

      COALESCE(SUM(d.trend IN ('^+', '^-', 'v^') AND d.trend_cnt <= 4), 0)
        AS young_bullish_count,
      COALESCE(SUM(d.trend IN ('^+', '^-', 'v^') AND d.trend_cnt >= 20), 0)
        AS mature_bullish_count
    FROM stdata.st_data AS d
    INNER JOIN (
      -- The preflights establish one effective mapping per industry_code and
      -- one effective name per sector_code.  This keeps a lone NULL display
      -- name NULL, while treating NULL and empty names as equivalent for checks.
      SELECT
        industry_code,
        MIN(sector_code) AS sector_code,
        CASE WHEN COUNT(sector_name) = 0 THEN NULL ELSE MIN(sector_name) END
          AS sector_name
      FROM stdata.st_listsectorsandindustries
      WHERE sector_code IS NOT NULL
      GROUP BY industry_code
    ) AS s
      ON s.industry_code = d.industry_id
    CROSS JOIN (
      SELECT 0 AS is_all_exchanges
      UNION ALL
      SELECT 1 AS is_all_exchanges
    ) AS scope
    WHERE d.weekdate = vWeekDate
      AND d.exchange IN ('A', 'N', 'Q', 'T')
      AND (
        (vType = 'CS' AND d.type = 'CS')
        OR (vType = 'EQ' AND d.type IN ('CS', 'UN'))
      )
    GROUP BY
      d.weekdate,
      CASE WHEN scope.is_all_exchanges = 1 THEN '*' ELSE d.exchange END,
      s.sector_code,
      s.sector_name
  ) AS a;

  -- Coverage begins before taxonomy exclusion.  The LEFT JOIN sees one mapping
  -- row per source industry_code after the earlier ambiguity checks.
  INSERT INTO stdata.st_sector_summary_coverage_shadow (
    weekdate,
    exchange,
    type,
    classified_population_count,
    mapped_classified_count,
    unmapped_classified_count
  )
  SELECT
    d.weekdate,
    CASE WHEN scope.is_all_exchanges = 1 THEN '*' ELSE d.exchange END AS exchange,
    vType,
    COUNT(*) AS classified_population_count,
    SUM(mapping.industry_code IS NOT NULL) AS mapped_classified_count,
    SUM(mapping.industry_code IS NULL) AS unmapped_classified_count
  FROM stdata.st_data AS d
  LEFT JOIN (
    SELECT industry_code
    FROM stdata.st_listsectorsandindustries
    WHERE sector_code IS NOT NULL
    GROUP BY industry_code
  ) AS mapping
    ON mapping.industry_code = d.industry_id
  CROSS JOIN (
    SELECT 0 AS is_all_exchanges
    UNION ALL
    SELECT 1 AS is_all_exchanges
  ) AS scope
  WHERE d.weekdate = vWeekDate
    AND d.exchange IN ('A', 'N', 'Q', 'T')
    AND d.trend IN ('^+', '^-', 'v^', '^v', 'v+', 'v-')
    AND (
      (vType = 'CS' AND d.type = 'CS')
      OR (vType = 'EQ' AND d.type IN ('CS', 'UN'))
    )
  GROUP BY
    d.weekdate,
    CASE WHEN scope.is_all_exchanges = 1 THEN '*' ELSE d.exchange END;
END$$

CREATE PROCEDURE stdata.UpdateSectorSummaryForwardShadow(
  IN dWeekDateFrom DATE,
  IN dWeekDateTo DATE,
  IN pType VARCHAR(4)
)
proc: BEGIN
  DECLARE vWeekDate DATE;

  IF dWeekDateFrom IS NULL OR dWeekDateTo IS NULL THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'dWeekDateFrom and dWeekDateTo are required';
  END IF;

  SET vWeekDate = DATE_ADD(
    dWeekDateFrom,
    INTERVAL MOD(6 - DAYOFWEEK(dWeekDateFrom) + 7, 7) DAY
  );

  WHILE vWeekDate <= dWeekDateTo DO
    CALL stdata.UpdateSectorSummaryNewShadow(vWeekDate, pType);
    SET vWeekDate = DATE_ADD(vWeekDate, INTERVAL 7 DAY);
  END WHILE;
END$$

DELIMITER ;

-- -------------------------------------------------------------------------
-- Read-only reconciliation templates (MySQL 8.4).
--
-- Required runs include 2026-09-25; an operator-selected Toronto UN week;
-- late-2006/early-2007; December 2022; and a pre-1993 raw reference.  The
-- shadow rebuild boundary remains 1993-01-01 unless separately approved.
-- Counts match exactly. DECIMAL(8,2) averages compare to ROUND(raw, 2),
-- bull_pct to ROUND(raw, 6), and leadership_score to ROUND(raw, 4).
-- -------------------------------------------------------------------------

-- mysql-client compatible operator parameters.  Change these for every run.
SET @weekdate = '2026-09-25';
SET @summary_type = 'EQ';  -- permitted values: CS, EQ

-- This must return zero rows before running either reconciliation template.
-- The producer raises rather than multiplying source rows if it finds one.
SELECT
  industry_code,
  COUNT(DISTINCT CONCAT(sector_code, CHAR(31), COALESCE(sector_name, '')))
    AS distinct_mapped_sector_definitions
FROM stdata.st_listsectorsandindustries
WHERE sector_code IS NOT NULL
GROUP BY industry_code
HAVING COUNT(DISTINCT CONCAT(sector_code, CHAR(31), COALESCE(sector_name, ''))) > 1;

-- Per-sector mapped-population reconciliation.  raw_all is an independent,
-- plain source aggregation for '*' and deliberately does not reuse the
-- producer's CROSS JOIN scope expansion.
WITH taxonomy_map AS (
  SELECT
    industry_code,
    MIN(sector_code) AS sector_code,
    CASE WHEN COUNT(sector_name) = 0 THEN NULL ELSE MIN(sector_name) END
      AS sector_name
  FROM stdata.st_listsectorsandindustries
  WHERE sector_code IS NOT NULL
  GROUP BY industry_code
), source_rows AS (
  SELECT
    d.weekdate, d.exchange, d.trend, d.trend_cnt, d.mt_cnt, d.rsi,
    s.sector_code, s.sector_name
  FROM stdata.st_data AS d
  INNER JOIN taxonomy_map AS s ON s.industry_code = d.industry_id
  WHERE d.weekdate = @weekdate
    AND d.exchange IN ('A', 'N', 'Q', 'T')
    AND ((@summary_type = 'CS' AND d.type = 'CS')
      OR (@summary_type = 'EQ' AND d.type IN ('CS', 'UN')))
), raw_by_exchange AS (
  SELECT
    weekdate, exchange, sector_code, sector_name,
    COUNT(*) AS observed_count,
    COALESCE(SUM(trend IN ('^+', '^-', 'v^', '^v', 'v+', 'v-')), 0) AS classified_count,
    COALESCE(SUM(trend IN ('^+', '^-', 'v^')), 0) AS bullish_count,
    COALESCE(SUM(trend IN ('^v', 'v+', 'v-')), 0) AS bearish_count,
    COALESCE(SUM(trend IN ('--', '=')), 0) AS neutral_count,
    AVG(CASE WHEN trend IN ('^+', '^-', 'v^', '^v', 'v+', 'v-') THEN trend_cnt END) AS raw_avg_trend_cnt,
    AVG(CASE WHEN trend IN ('^+', '^-', 'v^', '^v', 'v+', 'v-') THEN mt_cnt END) AS raw_avg_mt_cnt,
    AVG(CASE WHEN trend IN ('^+', '^-', 'v^', '^v', 'v+', 'v-') AND rsi IS NOT NULL AND rsi <= 10000 THEN rsi END) AS raw_avg_rsi,
    AVG(CASE WHEN trend IN ('^+', '^-', 'v^') AND rsi IS NOT NULL AND rsi <= 10000 THEN rsi END) AS raw_bull_avg_rsi
  FROM source_rows
  GROUP BY weekdate, exchange, sector_code, sector_name
), raw_all AS (
  SELECT
    weekdate, '*' AS exchange, sector_code, sector_name,
    COUNT(*) AS observed_count,
    COALESCE(SUM(trend IN ('^+', '^-', 'v^', '^v', 'v+', 'v-')), 0) AS classified_count,
    COALESCE(SUM(trend IN ('^+', '^-', 'v^')), 0) AS bullish_count,
    COALESCE(SUM(trend IN ('^v', 'v+', 'v-')), 0) AS bearish_count,
    COALESCE(SUM(trend IN ('--', '=')), 0) AS neutral_count,
    AVG(CASE WHEN trend IN ('^+', '^-', 'v^', '^v', 'v+', 'v-') THEN trend_cnt END) AS raw_avg_trend_cnt,
    AVG(CASE WHEN trend IN ('^+', '^-', 'v^', '^v', 'v+', 'v-') THEN mt_cnt END) AS raw_avg_mt_cnt,
    AVG(CASE WHEN trend IN ('^+', '^-', 'v^', '^v', 'v+', 'v-') AND rsi IS NOT NULL AND rsi <= 10000 THEN rsi END) AS raw_avg_rsi,
    AVG(CASE WHEN trend IN ('^+', '^-', 'v^') AND rsi IS NOT NULL AND rsi <= 10000 THEN rsi END) AS raw_bull_avg_rsi
  FROM source_rows
  GROUP BY weekdate, sector_code, sector_name
), raw_sector AS (
  SELECT * FROM raw_by_exchange
  UNION ALL
  SELECT * FROM raw_all
), shadow_sector AS (
  SELECT
    weekdate, exchange, sector_code, sector_name,
    total AS observed_count,
    bullish_count + bearish_count AS classified_count,
    bullish_count, bearish_count, neutral_count,
    avg_trend_cnt, avg_mt_cnt, avg_rsi, bull_avg_rsi,
    bull_pct, leadership_score
  FROM stdata.st_sector_summary_shadow
  WHERE weekdate = @weekdate AND type = @summary_type
), comparisons AS (
SELECT
  'raw_vs_shadow' AS reconciliation_side,
  r.weekdate, r.exchange, r.sector_code, r.sector_name,
  r.observed_count, r.classified_count, r.bullish_count, r.bearish_count,
  r.neutral_count,
  ROUND(r.raw_avg_trend_cnt, 2) AS raw_avg_trend_cnt,
  ROUND(r.raw_avg_mt_cnt, 2) AS raw_avg_mt_cnt,
  ROUND(r.raw_avg_rsi, 2) AS raw_avg_rsi,
  ROUND(r.raw_bull_avg_rsi, 2) AS raw_bull_avg_rsi,
  ROUND(r.bullish_count / NULLIF(r.bullish_count + r.bearish_count, 0), 6) AS raw_bull_pct,
  ROUND(
    r.raw_avg_rsi * (r.bullish_count / NULLIF(r.bullish_count + r.bearish_count, 0))
    + r.raw_avg_mt_cnt * 0.25,
    4
  ) AS raw_leadership_score,
  s.observed_count AS shadow_observed_count,
  s.classified_count AS shadow_classified_count,
  s.bullish_count AS shadow_bullish_count,
  s.bearish_count AS shadow_bearish_count,
  s.neutral_count AS shadow_neutral_count,
  s.avg_trend_cnt AS shadow_avg_trend_cnt,
  s.avg_mt_cnt AS shadow_avg_mt_cnt,
  s.avg_rsi AS shadow_avg_rsi,
  s.bull_avg_rsi AS shadow_bull_avg_rsi,
  s.bull_pct AS shadow_bull_pct,
  s.leadership_score AS shadow_leadership_score,
  (r.observed_count <=> s.observed_count
   AND r.classified_count <=> s.classified_count
   AND r.bullish_count <=> s.bullish_count
   AND r.bearish_count <=> s.bearish_count
   AND r.neutral_count <=> s.neutral_count) AS counts_match_exact,
  (ROUND(r.raw_avg_trend_cnt, 2) <=> s.avg_trend_cnt
   AND ROUND(r.raw_avg_mt_cnt, 2) <=> s.avg_mt_cnt
   AND ROUND(r.raw_avg_rsi, 2) <=> s.avg_rsi
   AND ROUND(r.raw_bull_avg_rsi, 2) <=> s.bull_avg_rsi) AS averages_match_rounded,
  (ROUND(r.bullish_count / NULLIF(r.bullish_count + r.bearish_count, 0), 6) <=> s.bull_pct) AS bull_pct_matches_raw,
  (ROUND(r.raw_avg_rsi * (r.bullish_count / NULLIF(r.bullish_count + r.bearish_count, 0)) + r.raw_avg_mt_cnt * 0.25, 4) <=> s.leadership_score) AS leadership_score_matches_raw
FROM raw_sector AS r
LEFT JOIN shadow_sector AS s
  ON s.weekdate = r.weekdate
 AND s.exchange = r.exchange
 AND s.sector_code = r.sector_code
UNION ALL
SELECT
  'shadow_without_raw' AS reconciliation_side,
  s.weekdate, s.exchange, s.sector_code, s.sector_name,
  NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
  s.observed_count, s.classified_count, s.bullish_count, s.bearish_count,
  s.neutral_count, s.avg_trend_cnt, s.avg_mt_cnt, s.avg_rsi, s.bull_avg_rsi,
  s.bull_pct, s.leadership_score,
  0, 0, 0, 0
FROM shadow_sector AS s
LEFT JOIN raw_sector AS r
  ON r.weekdate = s.weekdate
 AND r.exchange = s.exchange
 AND r.sector_code = s.sector_code
WHERE r.sector_code IS NULL
)
SELECT *
FROM comparisons
WHERE counts_match_exact = 0
   OR averages_match_rounded = 0
   OR bull_pct_matches_raw = 0
   OR leadership_score_matches_raw = 0
ORDER BY exchange, sector_code, reconciliation_side;

-- Coverage reconciliation also builds '*' independently from source rows.
WITH taxonomy_map AS (
  SELECT industry_code
  FROM stdata.st_listsectorsandindustries
  WHERE sector_code IS NOT NULL
  GROUP BY industry_code
), classified_source AS (
  SELECT d.weekdate, d.exchange, d.industry_id
  FROM stdata.st_data AS d
  WHERE d.weekdate = @weekdate
    AND d.exchange IN ('A', 'N', 'Q', 'T')
    AND d.trend IN ('^+', '^-', 'v^', '^v', 'v+', 'v-')
    AND ((@summary_type = 'CS' AND d.type = 'CS')
      OR (@summary_type = 'EQ' AND d.type IN ('CS', 'UN')))
), raw_coverage_by_exchange AS (
  SELECT
    d.weekdate, d.exchange,
    COUNT(*) AS classified_population_count,
    SUM(mapping.industry_code IS NOT NULL) AS mapped_classified_count,
    SUM(mapping.industry_code IS NULL) AS unmapped_classified_count
  FROM classified_source AS d
  LEFT JOIN taxonomy_map AS mapping ON mapping.industry_code = d.industry_id
  GROUP BY d.weekdate, d.exchange
), raw_coverage_all AS (
  SELECT
    d.weekdate, '*' AS exchange,
    COUNT(*) AS classified_population_count,
    SUM(mapping.industry_code IS NOT NULL) AS mapped_classified_count,
    SUM(mapping.industry_code IS NULL) AS unmapped_classified_count
  FROM classified_source AS d
  LEFT JOIN taxonomy_map AS mapping ON mapping.industry_code = d.industry_id
  GROUP BY d.weekdate
), raw_coverage AS (
  SELECT * FROM raw_coverage_by_exchange
  UNION ALL
  SELECT * FROM raw_coverage_all
), shadow_coverage AS (
  SELECT
    weekdate,
    exchange,
    classified_population_count,
    mapped_classified_count,
    unmapped_classified_count
  FROM stdata.st_sector_summary_coverage_shadow
  WHERE weekdate = @weekdate
    AND type = @summary_type
), coverage_comparisons AS (
SELECT
  'raw_vs_shadow' AS reconciliation_side,
  r.weekdate,
  r.exchange,
  r.classified_population_count AS raw_classified_population_count,
  r.mapped_classified_count AS raw_mapped_classified_count,
  r.unmapped_classified_count AS raw_unmapped_classified_count,
  s.classified_population_count AS shadow_classified_population_count,
  s.mapped_classified_count AS shadow_mapped_classified_count,
  s.unmapped_classified_count AS shadow_unmapped_classified_count,
  (r.classified_population_count <=> s.classified_population_count
   AND r.mapped_classified_count <=> s.mapped_classified_count
   AND r.unmapped_classified_count <=> s.unmapped_classified_count) AS counts_match_exact
FROM raw_coverage AS r
LEFT JOIN shadow_coverage AS s
 ON s.weekdate = r.weekdate
 AND s.exchange = r.exchange
UNION ALL
SELECT
  'shadow_without_raw' AS reconciliation_side,
  s.weekdate,
  s.exchange,
  NULL,
  NULL,
  NULL,
  s.classified_population_count,
  s.mapped_classified_count,
  s.unmapped_classified_count,
  0 AS counts_match_exact
FROM shadow_coverage AS s
LEFT JOIN raw_coverage AS r
 ON r.weekdate = s.weekdate
 AND r.exchange = s.exchange
WHERE r.exchange IS NULL
)
SELECT
  reconciliation_side,
  weekdate,
  exchange,
  raw_classified_population_count,
  raw_mapped_classified_count,
  raw_unmapped_classified_count,
  shadow_classified_population_count,
  shadow_mapped_classified_count,
  shadow_unmapped_classified_count,
  counts_match_exact
FROM coverage_comparisons
WHERE counts_match_exact = 0
ORDER BY exchange, reconciliation_side;

-- '*' count invariant. This independently proves that the direct shadow '*'
-- row has the same count-like components as the sum of shadow A/N/Q/T rows;
-- it deliberately does not attempt to validate any average by summation.
SELECT
  star.weekdate,
  star.type,
  star.sector_code,
  star.total AS star_total,
  parts.total AS reporting_total,
  star.bullish_count AS star_bullish_count,
  parts.bullish_count AS reporting_bullish_count,
  star.bearish_count AS star_bearish_count,
  parts.bearish_count AS reporting_bearish_count,
  star.neutral_count AS star_neutral_count,
  parts.neutral_count AS reporting_neutral_count
FROM stdata.st_sector_summary_shadow AS star
LEFT JOIN (
  SELECT
    weekdate, type, sector_code,
    SUM(total) AS total,
    SUM(bullish_count) AS bullish_count,
    SUM(bearish_count) AS bearish_count,
    SUM(neutral_count) AS neutral_count
  FROM stdata.st_sector_summary_shadow
  WHERE weekdate = @weekdate
    AND type = @summary_type
    AND exchange IN ('A', 'N', 'Q', 'T')
  GROUP BY weekdate, type, sector_code
) AS parts
  ON parts.weekdate = star.weekdate
 AND parts.type = star.type
 AND parts.sector_code = star.sector_code
WHERE star.weekdate = @weekdate
  AND star.type = @summary_type
  AND star.exchange = '*'
  AND NOT (
    star.total <=> parts.total
    AND star.bullish_count <=> parts.bullish_count
    AND star.bearish_count <=> parts.bearish_count
    AND star.neutral_count <=> parts.neutral_count
  )
ORDER BY star.sector_code;

-- CS control comparison against the existing production summary. This returns
-- only fields expected to stay unchanged; corrected averages, RSI diagnostics,
-- bull_pct, and leadership_score are intentionally excluded.
WITH cs_shadow AS (
  SELECT *
  FROM stdata.st_sector_summary_shadow
  WHERE weekdate = @weekdate
    AND type = 'CS'
    AND exchange IN ('A', 'N', 'Q', 'T')
), cs_comparisons AS (
SELECT
  'shadow_vs_production' AS reconciliation_side,
  shadow.weekdate,
  shadow.exchange,
  shadow.sector_code,
  shadow.total AS shadow_total,
  production.total AS production_total,
  shadow.newbulls, production.newbulls AS production_newbulls,
  shadow.bulls, production.bulls AS production_bulls,
  shadow.weakbulls, production.weakbulls AS production_weakbulls,
  shadow.newbears, production.newbears AS production_newbears,
  shadow.bears, production.bears AS production_bears,
  shadow.weakbears, production.weakbears AS production_weakbears,
  shadow.flats, production.flats AS production_flats,
  shadow.notrend, production.notrend AS production_notrend,
  shadow.bullish_count, production.bullish_count AS production_bullish_count,
  shadow.bearish_count, production.bearish_count AS production_bearish_count,
  shadow.neutral_count, production.neutral_count AS production_neutral_count,
  shadow.max_trend_cnt, production.max_trend_cnt AS production_max_trend_cnt,
  shadow.max_mt_cnt, production.max_mt_cnt AS production_max_mt_cnt,
  shadow.young_bullish_count, production.young_bullish_count AS production_young_bullish_count,
  shadow.mature_bullish_count, production.mature_bullish_count AS production_mature_bullish_count,
  (shadow.total <=> production.total
   AND shadow.newbulls <=> production.newbulls
   AND shadow.bulls <=> production.bulls
   AND shadow.weakbulls <=> production.weakbulls
   AND shadow.newbears <=> production.newbears
   AND shadow.bears <=> production.bears
   AND shadow.weakbears <=> production.weakbears
   AND shadow.flats <=> production.flats
   AND shadow.notrend <=> production.notrend
   AND shadow.bullish_count <=> production.bullish_count
   AND shadow.bearish_count <=> production.bearish_count
   AND shadow.neutral_count <=> production.neutral_count
   AND shadow.max_trend_cnt <=> production.max_trend_cnt
   AND shadow.max_mt_cnt <=> production.max_mt_cnt
   AND shadow.young_bullish_count <=> production.young_bullish_count
   AND shadow.mature_bullish_count <=> production.mature_bullish_count) AS fields_match_exact
FROM cs_shadow AS shadow
LEFT JOIN stdata.st_sector_summary AS production
  ON production.weekdate = shadow.weekdate
 AND production.exchange = shadow.exchange
 AND production.type = 'CS'
 AND production.sector_code = shadow.sector_code
UNION ALL
SELECT
  'production_without_shadow' AS reconciliation_side,
  production.weekdate,
  production.exchange,
  production.sector_code,
  NULL,
  production.total,
  NULL, production.newbulls,
  NULL, production.bulls,
  NULL, production.weakbulls,
  NULL, production.newbears,
  NULL, production.bears,
  NULL, production.weakbears,
  NULL, production.flats,
  NULL, production.notrend,
  NULL, production.bullish_count,
  NULL, production.bearish_count,
  NULL, production.neutral_count,
  NULL, production.max_trend_cnt,
  NULL, production.max_mt_cnt,
  NULL, production.young_bullish_count,
  NULL, production.mature_bullish_count,
  0 AS fields_match_exact
FROM stdata.st_sector_summary AS production
LEFT JOIN cs_shadow AS shadow
  ON shadow.weekdate = production.weekdate
 AND shadow.exchange = production.exchange
 AND shadow.sector_code = production.sector_code
WHERE production.weekdate = @weekdate
  AND production.type = 'CS'
  AND production.exchange IN ('A', 'N', 'Q', 'T')
  AND shadow.sector_code IS NULL
)
SELECT *
FROM cs_comparisons
WHERE fields_match_exact = 0
ORDER BY exchange, sector_code, reconciliation_side;
