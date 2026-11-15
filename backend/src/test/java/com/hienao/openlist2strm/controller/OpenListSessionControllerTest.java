package com.hienao.openlist2strm.controller;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.hienao.openlist2strm.config.security.Jwt;
import com.hienao.openlist2strm.config.security.UserDetailsServiceImpl;
import com.hienao.openlist2strm.service.AiBridgeService;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpMethod;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.userdetails.UsernameNotFoundException;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.springframework.web.client.HttpClientErrorException;

class OpenListSessionControllerTest {
  private final AiBridgeService bridge = mock(AiBridgeService.class);
  private final UserDetailsServiceImpl users = mock(UserDetailsServiceImpl.class);
  private final OpenListSessionController controller =
      new OpenListSessionController(
          bridge, new ObjectMapper(), new Jwt("fixture-session-secret", 60), users);

  @Test
  void verifiedAdministratorReceivesSignedSession() throws Exception {
    when(bridge.call(eq("/ostrm/session"), eq(HttpMethod.POST), any()))
        .thenReturn(ResponseEntity.ok("{\"username\":\"admin\",\"role\":2}".getBytes()));
    MockMvcBuilders.standaloneSetup(controller)
        .build()
        .perform(
            post("/api/auth/openlist-session")
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"token\":\"valid-admin\"}"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.data.username").value("admin"))
        .andExpect(jsonPath("$.data.token").isNotEmpty());
    verify(users).loadUserByUsername("admin");
  }

  @Test
  void browserCannotPromoteAnOrdinaryUser() throws Exception {
    when(bridge.call(any(), any(), any()))
        .thenReturn(ResponseEntity.ok("{\"username\":\"guest\",\"role\":0}".getBytes()));
    MockMvcBuilders.standaloneSetup(controller)
        .build()
        .perform(
            post("/api/auth/openlist-session")
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"token\":\"ordinary-user\",\"role\":2}"))
        .andExpect(status().isForbidden());
    verifyNoInteractions(users);
  }

  @Test
  void expiredOpenListTokenIsRejected() throws Exception {
    when(bridge.call(any(), any(), any()))
        .thenThrow(new HttpClientErrorException(HttpStatus.UNAUTHORIZED));
    MockMvcBuilders.standaloneSetup(controller)
        .build()
        .perform(
            post("/api/auth/openlist-session")
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"token\":\"expired\"}"))
        .andExpect(status().isUnauthorized());
  }

  @Test
  void unconfiguredLocalAdministratorIsNotIssuedASession() throws Exception {
    when(bridge.call(any(), any(), any()))
        .thenReturn(ResponseEntity.ok("{\"username\":\"other-admin\",\"role\":2}".getBytes()));
    when(users.loadUserByUsername("other-admin"))
        .thenThrow(new UsernameNotFoundException("missing"));
    MockMvcBuilders.standaloneSetup(controller)
        .build()
        .perform(
            post("/api/auth/openlist-session")
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"token\":\"other-admin-token\"}"))
        .andExpect(status().isForbidden());
  }
}
