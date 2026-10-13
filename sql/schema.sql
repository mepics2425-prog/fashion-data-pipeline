
/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;
DROP TABLE IF EXISTS `products`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `products` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `platform` varchar(30) NOT NULL COMMENT 'musinsa, 29cm …',
  `platform_product_id` varchar(50) NOT NULL COMMENT '플랫폼이 부여한 상품 ID(우리 id 와 구분)',
  `brand` varchar(150) NOT NULL COMMENT '실제 상품 브랜드. 판매 플랫폼명과 혼동 금지',
  `product_name` varchar(300) NOT NULL,
  `gender` varchar(20) DEFAULT NULL,
  `main_category` varchar(50) NOT NULL,
  `sub_category` varchar(80) DEFAULT NULL,
  `price` int DEFAULT NULL COMMENT '수집 시점 판매가(KRW)',
  `color` varchar(100) DEFAULT NULL,
  `material` varchar(300) DEFAULT NULL COMMENT '플랫폼 원문 그대로',
  `fit` varchar(80) DEFAULT NULL COMMENT '플랫폼 원문 그대로',
  `stretch` varchar(80) DEFAULT NULL,
  `thickness` varchar(80) DEFAULT NULL,
  `product_url` varchar(1000) NOT NULL,
  `search_keyword` varchar(100) DEFAULT NULL COMMENT '이 상품을 발견한 검색어. 공식 속성 아님',
  `raw` json DEFAULT NULL COMMENT '크롤러가 받은 원본 응답 전체',
  `collected_at` datetime NOT NULL,
  `updated_at` datetime DEFAULT NULL,
  `review_count` bigint unsigned DEFAULT NULL,
  `rating` decimal(4,2) DEFAULT NULL,
  `like_count` bigint unsigned DEFAULT NULL,
  `sales_count` bigint unsigned DEFAULT NULL,
  `popularity_rank` int unsigned DEFAULT NULL,
  `ranking_context` varchar(150) DEFAULT NULL,
  `is_new` tinyint(1) DEFAULT NULL,
  `trend_score` decimal(7,3) DEFAULT NULL,
  `trend_updated_at` datetime DEFAULT NULL,
  `style_tags` json DEFAULT NULL,
  `tpo_tags` json DEFAULT NULL,
  `season_tags` json DEFAULT NULL,
  `neckline` varchar(80) DEFAULT NULL,
  `silhouette` varchar(80) DEFAULT NULL,
  `pattern` varchar(80) DEFAULT NULL,
  `trend_raw` json DEFAULT NULL,
  `ontology_updated_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_product` (`platform`,`platform_product_id`),
  KEY `idx_category` (`main_category`,`sub_category`)
) ENGINE=InnoDB AUTO_INCREMENT=27674 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='수집 상품 기본정보';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `product_sizes`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `product_sizes` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `product_id` bigint NOT NULL,
  `size_label` varchar(30) NOT NULL COMMENT 'M, 28, FREE 등 표기 그대로',
  `measurement_type` varchar(40) NOT NULL COMMENT 'garment_actual | recommended_body | unknown',
  `total_length_cm` decimal(7,2) DEFAULT NULL,
  `shoulder_cm` decimal(7,2) DEFAULT NULL,
  `chest_flat_cm` decimal(7,2) DEFAULT NULL,
  `chest_circumference_cm` decimal(7,2) DEFAULT NULL,
  `sleeve_length_cm` decimal(7,2) DEFAULT NULL,
  `armhole_cm` decimal(7,2) DEFAULT NULL,
  `sleeve_width_cm` decimal(7,2) DEFAULT NULL,
  `waist_flat_cm` decimal(7,2) DEFAULT NULL,
  `waist_circumference_cm` decimal(7,2) DEFAULT NULL,
  `hip_flat_cm` decimal(7,2) DEFAULT NULL,
  `hip_circumference_cm` decimal(7,2) DEFAULT NULL,
  `thigh_flat_cm` decimal(7,2) DEFAULT NULL,
  `rise_cm` decimal(7,2) DEFAULT NULL,
  `inseam_cm` decimal(7,2) DEFAULT NULL,
  `hem_flat_cm` decimal(8,2) DEFAULT NULL,
  `extra` json DEFAULT NULL COMMENT '정식 컬럼에 없는 항목·플랫폼 고유 항목',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_size` (`product_id`,`size_label`,`measurement_type`),
  CONSTRAINT `fk_sizes_product` FOREIGN KEY (`product_id`) REFERENCES `products` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=91635 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='사이즈별 실측 치수';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `product_images`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `product_images` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `product_id` bigint NOT NULL,
  `image_order` int NOT NULL,
  `object_key` varchar(500) DEFAULT NULL COMMENT 'fashion/{platform}/{id}/{n}.{ext} — 전체 URL 이 아니라 키만',
  `source_url` varchar(1000) NOT NULL COMMENT '원본 이미지 주소. 재수집에 필요',
  `image_type` varchar(40) DEFAULT NULL COMMENT 'representative | detail | worn …',
  `bytes` int DEFAULT NULL,
  `uploaded_at` datetime DEFAULT NULL COMMENT 'NULL = 업로드 미완 → 재시도 대상',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_image` (`product_id`,`image_order`),
  CONSTRAINT `fk_images_product` FOREIGN KEY (`product_id`) REFERENCES `products` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=180261 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='상품 이미지 메타데이터';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `product_models`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `product_models` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `product_id` bigint NOT NULL,
  `height_cm` decimal(6,2) DEFAULT NULL,
  `wearing_size` varchar(30) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `fk_models_product` (`product_id`),
  CONSTRAINT `fk_models_product` FOREIGN KEY (`product_id`) REFERENCES `products` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=1533 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='상품 페이지에 표기된 모델 키·착용 사이즈';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `product_trend_snapshots`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `product_trend_snapshots` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `product_id` bigint NOT NULL,
  `captured_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `review_count` bigint unsigned DEFAULT NULL,
  `rating` decimal(4,2) DEFAULT NULL,
  `like_count` bigint unsigned DEFAULT NULL,
  `sales_count` bigint unsigned DEFAULT NULL,
  `popularity_rank` int unsigned DEFAULT NULL,
  `ranking_context` varchar(150) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_product_time` (`product_id`,`captured_at`),
  CONSTRAINT `fk_trend_product` FOREIGN KEY (`product_id`) REFERENCES `products` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=8192 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

