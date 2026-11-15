package com.hienao.openlist2strm.controller;

import com.hienao.openlist2strm.service.AiBridgeService;
import jakarta.servlet.http.HttpServletRequest;
import java.io.IOException;
import java.util.Map;
import lombok.RequiredArgsConstructor;
import org.springframework.http.HttpMethod;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.client.HttpStatusCodeException;
import org.springframework.web.client.RestClientException;

/** Uses OStrm's existing authenticated, single-administrator session. */
@RestController
@RequiredArgsConstructor
public class AiBridgeController {
  private final AiBridgeService bridge;

  @RequestMapping("/api/ai/**")
  public ResponseEntity<?> forward(HttpServletRequest request) throws IOException {
    String path = request.getRequestURI().substring("/api/ai".length());
    byte[] body = request.getInputStream().readNBytes(65537);
    if (body.length > 65536) {
      return ResponseEntity.status(413).body(Map.of("detail", "请求过大"));
    }
    if (!bridge.allowed(path)) {
      return ResponseEntity.status(404).body(Map.of("detail", "AI 接口不存在"));
    }
    try {
      ResponseEntity<byte[]> response =
          bridge.call(path, HttpMethod.valueOf(request.getMethod()), body);
      return ResponseEntity.status(response.getStatusCode())
          .contentType(org.springframework.http.MediaType.APPLICATION_JSON)
          .header("Cache-Control", "no-store")
          .body(response.getBody());
    } catch (HttpStatusCodeException error) {
      if (error.getStatusCode().value() == 401) {
        return ResponseEntity.status(502).body(Map.of("detail", "AI 服务连接凭据无效，请检查服务端配置"));
      }
      return ResponseEntity.status(error.getStatusCode())
          .contentType(org.springframework.http.MediaType.APPLICATION_JSON)
          .header("Cache-Control", "no-store")
          .body(error.getResponseBodyAsByteArray());
    } catch (IllegalStateException | IllegalArgumentException error) {
      return ResponseEntity.status(503).body(Map.of("detail", error.getMessage()));
    } catch (RestClientException error) {
      return ResponseEntity.status(502).body(Map.of("detail", "AI 服务暂时无法连接，请检查服务状态"));
    }
  }
}
