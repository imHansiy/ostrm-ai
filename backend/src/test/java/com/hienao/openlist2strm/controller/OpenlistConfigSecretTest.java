package com.hienao.openlist2strm.controller;

import static org.junit.jupiter.api.Assertions.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.hienao.openlist2strm.dto.openlist.OpenlistConfigDto;
import org.junit.jupiter.api.Test;

class OpenlistConfigSecretTest {
  @Test
  void sourceCredentialsAreWriteOnly() throws Exception {
    var mapper = new ObjectMapper();
    var dto =
        mapper.readValue(
            "{\"baseUrl\":\"https://files.example\",\"token\":\"private-token\"}",
            OpenlistConfigDto.class);
    assertEquals("private-token", dto.getToken());
    var output = mapper.readTree(mapper.writeValueAsBytes(dto));
    assertFalse(output.has("token"));
    assertTrue(output.path("tokenConfigured").asBoolean());
  }
}
