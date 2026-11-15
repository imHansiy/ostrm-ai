package com.hienao.openlist2strm.controller;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.hienao.openlist2strm.config.security.Jwt;
import com.hienao.openlist2strm.config.security.UserDetailsServiceImpl;
import com.hienao.openlist2strm.dto.ApiResponse;
import com.hienao.openlist2strm.service.AiBridgeService;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import java.io.IOException;
import java.util.Map;
import lombok.RequiredArgsConstructor;
import org.springframework.http.HttpMethod;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.userdetails.UsernameNotFoundException;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.client.HttpStatusCodeException;
import org.springframework.web.client.RestClientException;

/** Exchange an existing OpenList administrator session; never trust browser role claims. */
@RestController
@RequiredArgsConstructor
public class OpenListSessionController {
  private final AiBridgeService bridge;
  private final ObjectMapper mapper;
  private final Jwt jwt;
  private final UserDetailsServiceImpl users;

  public record Session(@NotBlank @Size(max = 4096) String token) {}

  @PostMapping("/api/auth/openlist-session")
  public ResponseEntity<?> login(
      @Valid @RequestBody Session body, HttpServletRequest request, HttpServletResponse response) {
    try {
      var result = bridge.call("/ostrm/session", HttpMethod.POST, mapper.writeValueAsBytes(body));
      var user = mapper.readTree(result.getBody());
      if (user.path("role").asInt() != 2) {
        return ResponseEntity.status(403).body(ApiResponse.error(403, "需要 OpenList 管理员权限"));
      }
      String username = user.path("username").asText("admin");
      users.loadUserByUsername(username);
      String token = jwt.makeToken(request, response, username);
      return ResponseEntity.ok(
          ApiResponse.success(
              Map.of("username", username, "token", token, "expiresAt", jwt.getExpiresAt(token))));
    } catch (UsernameNotFoundException error) {
      return ResponseEntity.status(403).body(ApiResponse.error(403, "请使用已配置的 OpenList 管理员账户"));
    } catch (HttpStatusCodeException error) {
      int status = error.getStatusCode().value() == 403 ? 403 : 401;
      return ResponseEntity.status(status).body(ApiResponse.error(status, "OpenList 登录无效或无管理权限"));
    } catch (RestClientException | IOException | IllegalStateException error) {
      return ResponseEntity.status(503).body(ApiResponse.error(503, "OpenList 登录桥接暂时不可用"));
    }
  }
}
