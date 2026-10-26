@echo off
echo ==============================================
echo Сборка Bedrock Cape Sync (SelfContained + FrameworkDependent)
echo ==============================================

echo [1/2] Сборка Self-Contained (со встроенным .NET)...
dotnet publish -c Release --self-contained true -p:EnableCompressionInSingleFile=true -r win-x64 -o publish\self-contained
if errorlevel 1 goto error
copy /y publish\self-contained\BedrockCapeSync.exe publish\BedrockCapeSync-SelfContained.exe >nul

echo [2/2] Сборка Framework-Dependent (легковесный)...
dotnet publish -c Release --self-contained false -p:EnableCompressionInSingleFile=false -r win-x64 -o publish\framework-dependent
if errorlevel 1 goto error
copy /y publish\framework-dependent\BedrockCapeSync.exe publish\BedrockCapeSync-FrameworkDependent.exe >nul

echo.
echo ==============================================
echo Успешно собрано в папку publish\:
echo 1. publish\BedrockCapeSync-SelfContained.exe   (не требует .NET)
echo 2. publish\BedrockCapeSync-FrameworkDependent.exe (требует .NET 8)
echo ==============================================
goto end

:error
echo.
echo [!] Ошибка во время сборки.

:end


pause