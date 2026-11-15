package com.hienao.openlist2strm.handler;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.hienao.openlist2strm.handler.context.FileProcessingContext;
import com.hienao.openlist2strm.service.AiBridgeService;
import com.hienao.openlist2strm.service.StrmFileService;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpMethod;
import org.springframework.stereotype.Component;

/** Registers shared-volume targets; AI failures never break original STRM generation. */
@Component
@Slf4j
public class AiMetadataHandler implements FileProcessorHandler {
  private final AiBridgeService bridge;
  private final StrmFileService strm;
  private final ObjectMapper mapper;
  private final Path root;
  private final boolean autoEnqueue;

  public AiMetadataHandler(
      AiBridgeService bridge,
      StrmFileService strm,
      ObjectMapper mapper,
      @Value("${app.paths.strm}") String root,
      @Value("${ai.service.auto-enqueue:false}") boolean autoEnqueue) {
    this.bridge = bridge;
    this.strm = strm;
    this.mapper = mapper;
    this.root = Path.of(root).toAbsolutePath().normalize();
    this.autoEnqueue = autoEnqueue;
  }

  @Override
  public ProcessingResult process(FileProcessingContext context) {
    if (!bridge.enabled() || context.getCurrentFile() == null) return ProcessingResult.SKIPPED;
    try {
      var task = context.getTaskConfig();
      var file = context.getCurrentFile();
      Path output =
          strm.resolveStrmFilePath(
                  task.getStrmPath(),
                  context.getRelativePath(),
                  file.getName(),
                  task.getRenameRegex())
              .toAbsolutePath()
              .normalize();
      if (!output.startsWith(root)) {
        log.warn("AI 资料输出目录需要位于配置的共享 STRM 根目录内");
        return ProcessingResult.SKIPPED;
      }
      String name = output.getFileName().toString().replaceFirst("\\.strm$", "");
      Map<String, Object> payload =
          new HashMap<>(
              Map.of(
                  "path", file.getPath(),
                  "directory", root.relativize(output.getParent()).toString().replace('\\', '/'),
                  "basename", name,
                  "source_base", context.getOpenlistConfig().getBaseUrl(),
                  "enqueue", autoEnqueue));
      if (file.getSize() != null) payload.put("source_size", file.getSize());
      if (file.getModified() != null) payload.put("source_modified", file.getModified());
      byte[] body = mapper.writeValueAsBytes(payload);
      bridge.call("/ostrm/metadata", HttpMethod.POST, body);
      return ProcessingResult.SUCCESS;
    } catch (Exception error) {
      // Do not log bodies, credentials, or private media URLs.
      log.warn("AI 资料尚未发布，可在 AI 中心重试，错误类别: {}", error.getClass().getSimpleName());
      return ProcessingResult.SKIPPED;
    }
  }

  @Override
  public int getOrder() {
    return 35;
  }

  @Override
  public Set<FileType> getHandledTypes() {
    return Set.of(FileType.VIDEO);
  }
}
