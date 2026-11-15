package com.hienao.openlist2strm.handler;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.hienao.openlist2strm.entity.OpenlistConfig;
import com.hienao.openlist2strm.entity.TaskConfig;
import com.hienao.openlist2strm.handler.context.FileProcessingContext;
import com.hienao.openlist2strm.service.AiBridgeService;
import com.hienao.openlist2strm.service.OpenlistApiService;
import com.hienao.openlist2strm.service.StrmFileService;
import java.nio.file.Path;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.mockito.ArgumentCaptor;
import org.springframework.http.HttpMethod;
import org.springframework.http.ResponseEntity;

class AiMetadataHandlerTest {
  @TempDir Path folder;

  @Test
  void schedulesRealSourcePathAndSharedSidecarTarget() throws Exception {
    AiBridgeService bridge = mock(AiBridgeService.class);
    when(bridge.enabled()).thenReturn(true);
    when(bridge.call(eq("/ostrm/metadata"), eq(HttpMethod.POST), any()))
        .thenReturn(ResponseEntity.ok(new byte[0]));
    StrmFileService strm = mock(StrmFileService.class);
    when(strm.resolveStrmFilePath(any(), any(), any(), any()))
        .thenReturn(folder.resolve("电影").resolve("片名.strm"));
    TaskConfig task = new TaskConfig();
    task.setStrmPath(folder.toString());
    OpenlistConfig source = new OpenlistConfig();
    source.setBaseUrl("https://openlist.example");
    OpenlistApiService.OpenlistFile file = new OpenlistApiService.OpenlistFile();
    file.setPath("/电影/片名.mp4");
    file.setName("片名.mp4");
    FileProcessingContext context =
        FileProcessingContext.builder()
            .taskConfig(task)
            .openlistConfig(source)
            .currentFile(file)
            .build();
    AiMetadataHandler handler =
        new AiMetadataHandler(bridge, strm, new ObjectMapper(), folder.toString(), true);
    assertEquals(55, handler.getOrder());
    assertEquals(ProcessingResult.SUCCESS, handler.process(context));
    ArgumentCaptor<byte[]> body = ArgumentCaptor.forClass(byte[].class);
    verify(bridge).call(eq("/ostrm/metadata"), eq(HttpMethod.POST), body.capture());
    var json = new ObjectMapper().readTree(body.getValue());
    assertEquals("/电影/片名.mp4", json.get("path").asText());
    assertEquals("电影", json.get("directory").asText());
    assertEquals("片名", json.get("basename").asText());
    assertTrue(json.get("enqueue").asBoolean());
  }

  @Test
  void disabledOrFailedAiNeverBreaksStrmProcessing() {
    AiBridgeService bridge = mock(AiBridgeService.class);
    AiMetadataHandler handler =
        new AiMetadataHandler(
            bridge, mock(StrmFileService.class), new ObjectMapper(), folder.toString(), false);
    assertEquals(ProcessingResult.SKIPPED, handler.process(new FileProcessingContext()));
    verify(bridge, never()).call(any(), any(), any());
  }
}
