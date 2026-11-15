package com.hienao.openlist2strm.service;

import java.net.URI;
import java.time.Duration;
import java.util.Set;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestTemplate;

/** Server-only bridge to the isolated AI worker. Browser tokens are never forwarded. */
@Service
public class AiBridgeService {
  private final String baseUrl;
  private final String bridgeKey;
  private final RestTemplate client;

  @Autowired
  public AiBridgeService(
      @Value("${ai.service.url:}") String baseUrl,
      @Value("${ai.service.bridge-key:}") String bridgeKey) {
    this(baseUrl, bridgeKey, createClient());
  }

  AiBridgeService(String baseUrl, String bridgeKey, RestTemplate client) {
    this.baseUrl = baseUrl.replaceAll("/+$", "");
    this.bridgeKey = bridgeKey;
    this.client = client;
    if (!this.baseUrl.isBlank()) {
      URI uri = URI.create(this.baseUrl);
      if (!Set.of("http", "https").contains(uri.getScheme())
          || uri.getHost() == null
          || uri.getUserInfo() != null
          || uri.getQuery() != null
          || uri.getFragment() != null) {
        throw new IllegalArgumentException("AI_SERVICE_URL must be an HTTP(S) origin");
      }
    }
  }

  private static RestTemplate createClient() {
    SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
    factory.setConnectTimeout(Duration.ofSeconds(5));
    factory.setReadTimeout(Duration.ofSeconds(200));
    return new RestTemplate(factory);
  }

  public boolean enabled() {
    return !baseUrl.isBlank() && !bridgeKey.isBlank();
  }

  public boolean allowed(String path) {
    return path.matches(
        "/(?:me|overview|settings|library|library/facets|library/availability|library/covers|"
            + "scan|recognize|jobs|jobs/[0-9]+/(?:pause|resume|cancel|retry)|"
            + "model/(?:test|key)|videos/(?:batch|edit)|tags|history|"
            + "exports/(?:publications(?:/[a-f0-9]{32}/(?:links|preview|sync|rotate|revoke))?|jobs)|"
            + "ostrm/(?:metadata|session))");
  }

  public ResponseEntity<byte[]> call(String path, HttpMethod method, byte[] body) {
    if (!enabled()) {
      throw new IllegalStateException("请配置 AI_SERVICE_URL 和 AI_BRIDGE_KEY");
    }
    if (!allowed(path)
        || !Set.of(HttpMethod.GET, HttpMethod.POST, HttpMethod.PUT).contains(method)) {
      throw new IllegalArgumentException("AI 接口未开放");
    }
    HttpHeaders headers = new HttpHeaders();
    headers.setContentType(MediaType.APPLICATION_JSON);
    headers.set("X-AI-Bridge", bridgeKey);
    return client.exchange(
        URI.create(baseUrl + "/ai/api" + path),
        method,
        new HttpEntity<>(body, headers),
        byte[].class);
  }
}
