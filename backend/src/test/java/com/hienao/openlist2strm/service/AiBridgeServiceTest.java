package com.hienao.openlist2strm.service;

import static org.junit.jupiter.api.Assertions.*;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.*;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withSuccess;

import org.junit.jupiter.api.Test;
import org.springframework.context.annotation.AnnotationConfigApplicationContext;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.test.web.client.MockRestServiceServer;
import org.springframework.web.client.RestTemplate;

class AiBridgeServiceTest {
  @Test
  void forwardsOnlyAllowedRoutesWithServerSideBridgeKey() {
    RestTemplate client = new RestTemplate();
    MockRestServiceServer server = MockRestServiceServer.bindTo(client).build();
    AiBridgeService service = new AiBridgeService("http://ai:8787", "test-bridge-key", client);
    server
        .expect(requestTo("http://ai:8787/ai/api/exports/publications"))
        .andExpect(method(HttpMethod.GET))
        .andExpect(header("X-AI-Bridge", "test-bridge-key"))
        .andExpect(headerDoesNotExist("Authorization"))
        .andRespond(withSuccess("{\"items\":[]}", MediaType.APPLICATION_JSON));
    assertEquals(
        200, service.call("/exports/publications", HttpMethod.GET, null).getStatusCode().value());
    assertFalse(service.allowed("/../../settings"));
    assertFalse(service.allowed("/settings?url=http://evil.example"));
    assertThrows(
        IllegalArgumentException.class, () -> service.call("/settings", HttpMethod.DELETE, null));
    server.verify();
  }

  @Test
  void springCreatesServiceWithDefaultDisabledConfiguration() {
    try (AnnotationConfigApplicationContext context = new AnnotationConfigApplicationContext()) {
      context.register(AiBridgeService.class);
      context.refresh();
      assertFalse(context.getBean(AiBridgeService.class).enabled());
    }
  }
}
