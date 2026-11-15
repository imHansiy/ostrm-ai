package com.hienao.openlist2strm.handler;

import static org.junit.jupiter.api.Assertions.assertEquals;

import com.hienao.openlist2strm.handler.context.FileProcessingContext;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.core.annotation.Order;

class HandlerOrderTest {
  private static final List<String> events = new ArrayList<>();

  @Order(30)
  static class Strm implements FileProcessorHandler {
    public ProcessingResult process(FileProcessingContext context) {
      events.add("strm");
      return ProcessingResult.SUCCESS;
    }
  }

  @Order(50)
  static class Nfo implements FileProcessorHandler {
    public ProcessingResult process(FileProcessingContext context) {
      events.add("nfo");
      return ProcessingResult.SUCCESS;
    }
  }

  @Order(55)
  static class Ai implements FileProcessorHandler {
    public ProcessingResult process(FileProcessingContext context) {
      events.add("ai");
      return ProcessingResult.SUCCESS;
    }
  }

  @Order(60)
  static class Cleanup implements FileProcessorHandler {
    public ProcessingResult process(FileProcessingContext context) {
      events.add("cleanup");
      return ProcessingResult.SUCCESS;
    }
  }

  @Test
  void annotationOrderIsHonoredAndAiRunsAfterExistingNfo() {
    events.clear();
    var chain = new FileProcessorChain(List.of(new Cleanup(), new Ai(), new Strm(), new Nfo()));
    assertEquals(ProcessingResult.SUCCESS, chain.execute(new FileProcessingContext()));
    assertEquals(List.of("strm", "nfo", "ai", "cleanup"), events);
  }
}
