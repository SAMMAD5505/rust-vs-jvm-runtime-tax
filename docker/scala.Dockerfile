# JVM Scala image: pipeline-scala fat jar on Temurin 21.0.4+7.
# The runtime stage is a full JDK so `jfr` is available for parsing recordings.
# Build from the repository root: docker build -f docker/scala.Dockerfile -t rvj-scala .

# eclipse-temurin:21.0.4_7-jdk-jammy
FROM eclipse-temurin@sha256:0472478e22da0f66043fa6acd8cd30126592349f47937adafc2340794e5bf06a AS build
ARG SBT_VERSION=1.10.2
ARG SBT_SHA256=a716dd018bd68bc7a95a2dd10337663aa76f443ad6c99deabe5eadd1adfc7639
RUN curl -fsSL -o /tmp/sbt.tgz "https://github.com/sbt/sbt/releases/download/v${SBT_VERSION}/sbt-${SBT_VERSION}.tgz" \
 && echo "${SBT_SHA256}  /tmp/sbt.tgz" | sha256sum -c - \
 && tar xzf /tmp/sbt.tgz -C /opt && rm /tmp/sbt.tgz
ENV PATH="/opt/sbt/bin:${PATH}"
WORKDIR /src
COPY pipeline-scala/project project
COPY pipeline-scala/build.sbt .
RUN sbt -batch update
COPY pipeline-scala/src src
RUN sbt -batch test assembly

FROM eclipse-temurin@sha256:0472478e22da0f66043fa6acd8cd30126592349f47937adafc2340794e5bf06a
ARG JDK_BUILD=Temurin-21.0.4+7
# Fail the build unless the JDK that runs the benchmark is exactly the pinned build.
RUN java -version 2>&1 | tee /dev/stderr | grep -q "Runtime Environment ${JDK_BUILD} "
COPY --from=build /src/target/scala-3.5.0/pipeline-scala.jar /opt/rvj/pipeline-scala.jar
COPY profiling/gc.jfc /opt/rvj/gc.jfc
COPY docker/run-wrapper.sh /usr/local/bin/run-wrapper
RUN chmod 0755 /usr/local/bin/run-wrapper
LABEL org.opencontainers.image.source="https://github.com/manojkumargoudsowdari/rust-vs-jvm-runtime-tax" \
      rvj.jdk="21.0.4+7" rvj.scala="3.5.0"
ENTRYPOINT ["/usr/local/bin/run-wrapper"]
